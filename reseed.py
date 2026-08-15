"""Auto-reseed miniapps.ai credentials from a captured browser request.

Copy the request from browser DevTools (Network tab, right-click any
`api.miniapps.ai` request -> Copy as cURL / Copy as Python requests) and paste
it into a file. Any authenticated request works, because every one of them
carries the three things the proxy needs:

  * `jwt` cookie                          -> the session token
  * `__Host-miniapps.x-csrf-token` cookie -> the CSRF pair, cookie half
  * `x-csrf-token` header                 -> the CSRF pair, header half

`__cf_bm` is picked up too when present, but it is optional: Cloudflare hands
out a fresh one on the first request.

Everything is matched by name and by JWT claims, so field order, quoting style,
and extra analytics cookies do not matter.

Usage:
    python reseed.py capture.txt
    python reseed.py capture.txt --json
    python reseed.py capture.txt --write-env .env
    python reseed.py capture.txt --push https://host --key sk-...
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# Cookie/header names as they appear in a capture.
_JWT_COOKIE = "jwt"
_CSRF_COOKIE = "__Host-miniapps.x-csrf-token"
_CSRF_HEADER = "x-csrf-token"
_CF_BM_COOKIE = "__cf_bm"

# Required to talk to the API at all.
_REQUIRED = ("jwt", "csrf_token", "csrf_cookie")
# Nice to have; regenerated automatically when absent.
_OPTIONAL = ("cf_bm",)

_ENV_KEYS = [
    ("MINIAPPS_JWT", "jwt"),
    ("MINIAPPS_CSRF_TOKEN", "csrf_token"),
    ("MINIAPPS_CSRF_COOKIE", "csrf_cookie"),
    ("MINIAPPS_CF_BM", "cf_bm"),
    ("MINIAPPS_USER_EMAIL", "user_email"),
]


def _find_value(name: str, text: str) -> Optional[str]:
    """Find a cookie/header value in any of the shapes DevTools produces.

    Three styles are supported, tried in order:
      1. python-requests dict:  'x-csrf-token': 'value'
      2. cookie string:         __Host-miniapps.x-csrf-token=value; jwt=value
      3. cURL header:           -H 'x-csrf-token: value'

    The name must match exactly and be preceded by a real delimiter. That is
    what stops a lookup of `x-csrf-token` from returning the value of
    `__Host-miniapps.x-csrf-token`, whose name merely ends with it.
    """
    patterns = [
        r"['\"]" + re.escape(name) + r"['\"]\s*[:=]\s*['\"]([^'\"]+)['\"]",
        r"(?:^|[;,\s&'\"(])" + re.escape(name) + r"=([^;'\"\s&]+)",
        r"(?:^|[;,\s'\"(])" + re.escape(name) + r"\s*:\s*([^;'\"\r\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            return match.group(1).strip().strip("'\"").rstrip("\\").strip()
    return None


def _b64json(segment: str) -> Dict[str, Any]:
    segment += "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))


def decode_jwt(token: str) -> Dict[str, Any]:
    """Return a JWT's claims, or an empty dict when it cannot be decoded."""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        return _b64json(parts[1])
    except Exception:
        return {}


def _best_session_jwt(text: str) -> tuple[Optional[str], Dict[str, Any]]:
    """Pick the freshest miniapps session JWT in a capture.

    A miniapps session token carries `id` and `email` claims. Selecting by
    claims rather than by position means a capture that also contains unrelated
    tokens still resolves correctly.
    """
    best: Optional[str] = None
    best_claims: Dict[str, Any] = {}
    for token in _JWT_RE.findall(text):
        claims = decode_jwt(token)
        if not claims or not ("id" in claims or "email" in claims):
            continue
        if best is None or claims.get("iat", 0) >= best_claims.get("iat", 0):
            best, best_claims = token, claims
    return best, best_claims


def parse_captured_request(text: str) -> Dict[str, Any]:
    """Extract every proxy credential found in a captured request."""
    session_jwt, claims = _best_session_jwt(text)
    jwt = session_jwt or _find_value(_JWT_COOKIE, text)
    if not claims and jwt:
        claims = decode_jwt(jwt)

    expires_at = claims.get("exp")
    parsed: Dict[str, Any] = {
        "jwt": jwt,
        "csrf_token": _find_value(_CSRF_HEADER, text),
        "csrf_cookie": _find_value(_CSRF_COOKIE, text),
        "cf_bm": _find_value(_CF_BM_COOKIE, text),
        "user_id": claims.get("id"),
        "user_email": claims.get("email"),
        "issued_at": _iso(claims.get("iat")),
        "expires_at": _iso(expires_at),
    }
    parsed["_missing"] = [name for name in _REQUIRED if not parsed.get(name)]
    parsed["_missing_optional"] = [name for name in _OPTIONAL if not parsed.get(name)]
    parsed["_expired"] = bool(
        expires_at and expires_at < datetime.now(tz=timezone.utc).timestamp()
    )
    return parsed


def _iso(epoch: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def to_env_block(parsed: Dict[str, Any]) -> str:
    return "\n".join(
        f"{env}={parsed[key]}" for env, key in _ENV_KEYS if parsed.get(key)
    )


def _write_env(path: str, parsed: Dict[str, Any]) -> None:
    from pathlib import Path

    updates = dict(line.split("=", 1) for line in to_env_block(parsed).splitlines())
    target = Path(path)
    existing = target.read_text("utf-8").splitlines() if target.is_file() else []
    out, seen = [], set()
    for line in existing:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    target.write_text("\n".join(out) + "\n", "utf-8")


def _push(base: str, key: str, text: str) -> None:
    import requests

    resp = requests.post(
        f"{base.rstrip('/')}/auth/reseed",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"raw": text},
        timeout=30,
    )
    print(f"POST /auth/reseed -> {resp.status_code}")
    print(resp.text[:1000])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reseed miniapps.ai credentials from a captured browser request."
    )
    parser.add_argument("file", help="File containing the cURL or Python-requests capture")
    parser.add_argument("--json", action="store_true", help="Print the parsed result as JSON")
    parser.add_argument("--push", metavar="BASE_URL", help="POST the capture to a live /auth/reseed")
    parser.add_argument("--key", help="API key, required with --push")
    parser.add_argument("--write-env", metavar="PATH", help="Update the MINIAPPS_* keys in this .env")
    args = parser.parse_args(argv)

    with open(args.file, encoding="utf-8") as handle:
        text = handle.read()
    parsed = parse_captured_request(text)

    if args.json:
        print(json.dumps(parsed, indent=2))
    else:
        print("Parsed credentials:")
        for env, key in _ENV_KEYS:
            value = parsed.get(key)
            shown = (value[:40] + "...") if value and len(value) > 40 else (value or "")
            flag = "OK     " if value else ("MISSING" if key in _REQUIRED else "absent ")
            print(f"  {env:22} {flag}  {shown}")
        print(f"\n  session user   {parsed.get('user_email') or '?'} ({parsed.get('user_id') or '?'})")
        print(f"  issued at      {parsed.get('issued_at') or '?'}")
        print(f"  expires at     {parsed.get('expires_at') or '?'}")

    if parsed["_missing"]:
        print("\nERROR: required fields missing:", ", ".join(parsed["_missing"]))
        print("This capture does not identify a session. Re-copy the request.")
        return 1

    if parsed["_expired"]:
        print(
            "\nWARNING: this JWT is already expired. miniapps.ai exposes no refresh\n"
            "endpoint, so sign in again and re-copy a fresh request."
        )

    if not args.json:
        print("\n--- env block (paste into Railway / .env) ---")
        print(to_env_block(parsed))

    if args.write_env:
        _write_env(args.write_env, parsed)
        print(f"\nWrote {args.write_env}")
    if args.push:
        if not args.key:
            parser.error("--push requires --key")
        _push(args.push, args.key, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
