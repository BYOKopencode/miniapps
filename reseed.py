"""Parse a captured miniapps.ai request into reusable session credentials.

Usable as a library (``parse_captured_request``) and as a CLI:

    python reseed.py capture.txt --json
    python reseed.py capture.txt --write-env .env
    python reseed.py capture.txt --push https://your-app.up.railway.app --key $MINIAPPS_API_KEY

miniapps.ai has no token refresh endpoint, so "reseeding" a fresh browser
capture is the supported way to renew a session.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+")

_JWT_COOKIE = "jwt"
_CSRF_COOKIE = "__Host-miniapps.x-csrf-token"
_CSRF_HEADER = "x-csrf-token"
_CF_BM_COOKIE = "__cf_bm"

_REQUIRED = ("jwt", "csrf_token", "csrf_cookie")
_OPTIONAL = ("cf_bm",)

_ENV_KEYS = {
    "jwt": "MINIAPPS_JWT",
    "csrf_token": "MINIAPPS_CSRF_TOKEN",
    "csrf_cookie": "MINIAPPS_CSRF_COOKIE",
    "cf_bm": "MINIAPPS_CF_BM",
    "user_email": "MINIAPPS_USER_EMAIL",
}


def _find_value(text: str, name: str) -> str:
    """Find a cookie/header value in any of the shapes DevTools produces.

    Three styles are supported:

    1. object literals - ``'jwt': 'eyJ...'``
    2. cookie strings  - ``jwt=eyJ...; __cf_bm=...``
    3. cURL headers    - ``-H 'x-csrf-token: 5be...'``

    The order matters: the cookie pattern is tried before the colon pattern so
    a request for ``x-csrf-token`` can never return the ``__Host-`` cookie.
    """
    boundary = r"(?:^|[;,\s'\"({])"
    patterns = (
        r"['\"]" + re.escape(name) + r"['\"]\s*[:=]\s*['\"]([^'\"]+)['\"]",
        boundary + re.escape(name) + r"\s*=\s*([^;'\"\s,]+)",
        boundary + re.escape(name) + r"\s*:\s*([^;'\"\r\n]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            return match.group(1).strip().strip("'\"").rstrip("\\").strip()
    return ""


def _b64json(segment: str) -> Dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def decode_jwt(token: str) -> Dict[str, Any]:
    """Decode a JWT payload without verifying its signature.

    Only used to read `id`, `email`, `iat`, and `exp` for status reporting;
    miniapps.ai remains the authority on whether a token is accepted.
    """
    if not token or token.count(".") != 2:
        return {}
    return _b64json(token.split(".")[1])


def _best_session_jwt(text: str) -> str:
    """Pick the session token when a capture contains several JWT-shaped values."""
    named = _find_value(text, _JWT_COOKIE)
    if named and named.count(".") == 2:
        return named
    candidates: List[str] = _JWT_RE.findall(text)
    for candidate in candidates:
        claims = decode_jwt(candidate)
        if claims.get("id") or claims.get("email"):
            return candidate
    return candidates[0] if candidates else ""


def parse_captured_request(raw: str) -> Dict[str, Any]:
    """Extract credentials + session metadata from a pasted request.

    Never raises on bad input: callers decide how to treat `_missing`.
    """
    text = raw or ""
    values: Dict[str, Any] = {
        "jwt": _best_session_jwt(text),
        "csrf_token": _find_value(text, _CSRF_HEADER),
        "csrf_cookie": _find_value(text, _CSRF_COOKIE),
        "cf_bm": _find_value(text, _CF_BM_COOKIE),
    }

    # The header and cookie CSRF values are different strings; if only one was
    # found, do not silently reuse it for the other.
    claims = decode_jwt(str(values["jwt"]))
    values["user_id"] = str(claims.get("id") or "")
    values["user_email"] = str(claims.get("email") or "")
    values["issued_at"] = _iso(claims.get("iat"))
    values["expires_at"] = _iso(claims.get("exp"))

    exp = claims.get("exp")
    values["_expired"] = bool(isinstance(exp, (int, float)) and exp < time.time())
    values["_missing"] = [field for field in _REQUIRED if not values.get(field)]
    values["_missing_optional"] = [field for field in _OPTIONAL if not values.get(field)]
    return values


def _iso(epoch: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def to_env_block(values: Dict[str, Any]) -> str:
    """Render parsed credentials as MINIAPPS_* environment lines."""
    lines = []
    for field, env_key in _ENV_KEYS.items():
        value = values.get(field)
        if value:
            lines.append(f"{env_key}={value}")
    return "\n".join(lines)


def _write_env(path: Path, values: Dict[str, Any]) -> List[str]:
    """Upsert the MINIAPPS_* lines in an .env file, leaving other keys alone."""
    updates = {
        env_key: str(values[field])
        for field, env_key in _ENV_KEYS.items()
        if values.get(field)
    }
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    out: List[str] = []
    seen = set()
    for line in existing:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return sorted(updates)


def _push(base_url: str, api_key: Optional[str], values: Dict[str, Any]) -> Dict[str, Any]:
    """POST the parsed credentials to a running instance's /auth/reseed."""
    import requests

    payload = {
        field: values[field]
        for field in ("jwt", "csrf_token", "csrf_cookie", "cf_bm", "user_id", "user_email")
        if values.get(field)
    }
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    response = requests.post(
        f"{base_url.rstrip('/')}/auth/reseed", json=payload, headers=headers, timeout=30
    )
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return {"status": response.status_code, "body": body}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn a captured miniapps.ai request into session credentials."
    )
    parser.add_argument(
        "capture",
        nargs="?",
        help="File containing the copied request. Reads stdin when omitted.",
    )
    parser.add_argument("--json", action="store_true", help="Print parsed values as JSON.")
    parser.add_argument("--write-env", metavar="PATH", help="Upsert MINIAPPS_* keys into this .env file.")
    parser.add_argument("--push", metavar="BASE_URL", help="POST the credentials to a running instance.")
    parser.add_argument("--key", help="API key to use with --push.")
    args = parser.parse_args(argv)

    if args.capture:
        path = Path(args.capture)
        if not path.is_file():
            print(f"No such file: {path}", file=sys.stderr)
            return 2
        raw = path.read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()

    values = parse_captured_request(raw)

    if args.json:
        print(json.dumps(values, indent=2))
    else:
        for field in (*_REQUIRED, *_OPTIONAL):
            value = values.get(field)
            state = f"{str(value)[:12]}... ({len(str(value))} chars)" if value else "MISSING"
            print(f"{field:12} {state}")
        if values.get("user_email") or values.get("user_id"):
            print(f"session user  {values.get('user_email')} ({values.get('user_id')})")
        if values.get("issued_at"):
            print(f"issued at     {values['issued_at']}")
        if values.get("expires_at"):
            print(f"expires at    {values['expires_at']}")

    if values["_missing"]:
        print(
            "\nMissing required values: " + ", ".join(values["_missing"]) + ".\n"
            "Copy an authenticated request to api.miniapps.ai (right-click a request in\n"
            "DevTools > Network, then Copy as cURL or Copy as fetch).",
            file=sys.stderr,
        )
        return 1
    if values["_expired"]:
        print(
            f"\nWarning: this jwt already expired at {values['expires_at']}.", file=sys.stderr
        )
    if values["_missing_optional"]:
        print(
            "Note: no __cf_bm cookie in this capture. That is fine - Cloudflare issues a "
            "fresh one on the first call.",
            file=sys.stderr,
        )

    if args.write_env:
        written = _write_env(Path(args.write_env), values)
        print(f"\nWrote {', '.join(written)} to {args.write_env}")

    if args.push:
        result = _push(args.push, args.key, values)
        print("\n" + json.dumps(result, indent=2))
        return 0 if 200 <= int(result["status"]) < 300 else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
