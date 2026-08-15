"""Core miniapps.ai proxy: browser-session auth and conversation REST calls."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests

from config import UserConfig, settings
from reseed import decode_jwt

# The six tool toggles exposed by a miniapps.ai conversation.
TOOL_KEYS = (
    "webSearch",
    "codeInterpreter",
    "canvas",
    "flightSearch",
    "locationSearch",
    "conversationLookup",
)


def extract_tool_settings(payload: Any) -> Dict[str, bool]:
    """Pull `toolSettings` out of a conversation payload, wherever it is nested."""
    if isinstance(payload, dict):
        for node in (payload, payload.get("conversation"), payload.get("data")):
            if isinstance(node, dict) and isinstance(node.get("toolSettings"), dict):
                return {
                    key: bool(value)
                    for key, value in node["toolSettings"].items()
                    if key in TOOL_KEYS
                }
    return {}


class SessionExpired(RuntimeError):
    """The seeded `jwt` cookie is past its expiry and cannot be rotated."""


class MiniAppsProxy:
    """Holds one user's miniapps.ai session and proxies REST calls to it.

    Thread-safe. Unlike a Clerk-style backend, miniapps.ai exposes no token
    refresh endpoint: the `jwt` cookie is a long-lived HS256 token (~15 days).
    So `ensure_fresh` validates expiry instead of rotating, and `/auth/reseed`
    swaps in a new capture once it does expire.
    """

    def __init__(self, user: UserConfig):
        self._lock = threading.Lock()

        # Identity
        self.name = user.name
        claims = decode_jwt(user.jwt)
        self.user_id = user.user_id or str(claims.get("id") or "")
        self.user_email = user.user_email or str(claims.get("email") or "")

        # Credentials
        self._jwt = user.jwt
        self._csrf_token = user.csrf_token
        self._csrf_cookie = user.csrf_cookie
        self._cf_bm = user.cf_bm

        # Endpoints
        self.api_base = settings.miniapps_api_base.rstrip("/")
        self.frontend_base = settings.miniapps_frontend_base.rstrip("/")
        self.user_agent = settings.miniapps_user_agent
        self.timeout = settings.request_timeout

        self._http = requests.Session()

    # ── Cookie / header builders ──────────────────────────────────────

    def _cookies(self) -> Dict[str, str]:
        jar = {
            "jwt": self._jwt,
            "__Host-miniapps.x-csrf-token": self._csrf_cookie,
            "__cf_bm": self._cf_bm,
        }
        return {name: value for name, value in jar.items() if value}

    def _headers(self) -> Dict[str, str]:
        """Browser-shaped headers. The CSRF header must pair with its cookie."""
        return {
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": self.frontend_base,
            "priority": "u=1, i",
            "referer": f"{self.frontend_base}/",
            "sec-ch-ua": '"Not=A?Brand";v="99", "Brave";v="151", "Chromium";v="151"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "sec-gpc": "1",
            "user-agent": self.user_agent,
            "x-csrf-token": self._csrf_token,
        }

    # ── JWT helpers ───────────────────────────────────────────────────

    def _claims(self) -> Dict[str, Any]:
        return decode_jwt(self._jwt)

    def _expiry(self) -> Optional[int]:
        exp = self._claims().get("exp")
        return int(exp) if isinstance(exp, (int, float)) else None

    def _is_expired(self, buffer: int = 30) -> bool:
        """Whether the session token is unusable for the next outbound call.

        A missing/undecodable `exp` is treated as usable: the upstream is the
        real authority, and failing closed here would break captures whose token
        format changes.
        """
        exp = self._expiry()
        if exp is None:
            return False
        return (exp - buffer) < time.time()

    def ensure_fresh(self, force: bool = False) -> None:
        """Validate the session before an outbound call.

        There is nothing to rotate, so `force` only makes the expiry check
        loud: it raises instead of letting the upstream answer with a 401.
        """
        with self._lock:
            if self._is_expired():
                raise SessionExpired(
                    "The miniapps `jwt` cookie expired at "
                    f"{self._expiry_iso() or 'an unknown time'}. miniapps.ai has no refresh "
                    "endpoint: sign in again, copy a fresh request, and POST it to "
                    "/auth/reseed (or update MINIAPPS_JWT and redeploy)."
                )
            if force:
                return

    def _expiry_iso(self) -> Optional[str]:
        exp = self._expiry()
        return datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None

    def _absorb_cloudflare_cookie(self) -> None:
        """Keep whatever `__cf_bm` Cloudflare last issued.

        The cookie lives ~30 minutes, so reusing a stale seeded value is worse
        than reusing the fresh one from the session jar.
        """
        issued = self._http.cookies.get("__cf_bm")
        if issued and issued != self._cf_bm:
            self._cf_bm = issued

    # ── Core request ──────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any]:
        """Call the upstream API and return (status_code, parsed body).

        Raises `requests.HTTPError` for 4xx/5xx so callers can map the upstream
        status straight through.
        """
        self.ensure_fresh()
        url = path if path.startswith("http") else f"{self.api_base}/{path.lstrip('/')}"
        response = self._http.request(
            method.upper(),
            url,
            headers=self._headers(),
            cookies=self._cookies(),
            json=json_body,
            params=params or None,
            timeout=self.timeout,
        )
        # Upstream sends UTF-8 but does not always declare a charset, and
        # requests then falls back to ISO-8859-1, mangling non-ASCII text.
        response.encoding = "utf-8"
        self._absorb_cloudflare_cookie()
        response.raise_for_status()
        return response.status_code, _parse_body(response)

    # ── Conversations ─────────────────────────────────────────────────

    def get_conversation(self, conversation_id: str) -> Any:
        return self.request("GET", f"/conversations/{conversation_id}")[1]

    def patch_conversation(self, conversation_id: str, patch: Dict[str, Any]) -> Any:
        return self.request("PUT", f"/conversations/{conversation_id}", json_body=patch)[1]

    def get_tool_settings(self, conversation_id: str) -> Dict[str, bool]:
        return extract_tool_settings(self.get_conversation(conversation_id))

    def set_tool_settings(
        self, conversation_id: str, desired: Dict[str, bool], merge: bool = True
    ) -> Dict[str, Any]:
        """Update tool toggles.

        The upstream PUT replaces the whole `toolSettings` object, so a partial
        update has to read current state first. `merge=False` reproduces the
        raw browser behaviour: unlisted flags go to false.
        """
        payload: Dict[str, bool] = {key: False for key in TOOL_KEYS}
        if merge:
            try:
                payload.update(self.get_tool_settings(conversation_id))
            except requests.HTTPError:
                # GET may not be exposed for this account; fall back to the
                # explicit flags rather than failing the whole write.
                pass
        payload.update(desired)
        upstream = self.patch_conversation(conversation_id, {"toolSettings": payload})
        return {
            "conversationId": conversation_id,
            "toolSettings": payload,
            "upstream": upstream,
        }

    # ── Public helpers ────────────────────────────────────────────────

    def update_cookies(
        self,
        jwt: Optional[str] = None,
        csrf_token: Optional[str] = None,
        csrf_cookie: Optional[str] = None,
        cf_bm: Optional[str] = None,
    ) -> None:
        with self._lock:
            if jwt is not None:
                self._jwt = jwt
            if csrf_token is not None:
                self._csrf_token = csrf_token
            if csrf_cookie is not None:
                self._csrf_cookie = csrf_cookie
            if cf_bm is not None:
                self._cf_bm = cf_bm
                self._http.cookies.pop("__cf_bm", None)

    def reseed(
        self,
        jwt: Optional[str] = None,
        csrf_token: Optional[str] = None,
        csrf_cookie: Optional[str] = None,
        cf_bm: Optional[str] = None,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
        verify: bool = True,
    ) -> None:
        """Replace identity + credentials from a fresh capture."""
        self.update_cookies(
            jwt=jwt, csrf_token=csrf_token, csrf_cookie=csrf_cookie, cf_bm=cf_bm
        )
        with self._lock:
            claims = decode_jwt(self._jwt)
            self.user_id = user_id or str(claims.get("id") or "") or self.user_id
            self.user_email = user_email or str(claims.get("email") or "") or self.user_email
        if verify:
            self.ensure_fresh(force=True)

    def status(self) -> Dict[str, Any]:
        claims = self._claims()
        exp = self._expiry()
        return {
            "name": self.name,
            "user_id": self.user_id,
            "user_email": self.user_email,
            "jwt_issued_at": _iso(claims.get("iat")),
            "jwt_expires_at": _iso(exp),
            "jwt_expired": self._is_expired(),
            "jwt_seconds_remaining": max(0, int(exp - time.time())) if exp else None,
            "has_cf_bm": bool(self._cf_bm),
            "api_base": self.api_base,
        }

    def export_env(self) -> Dict[str, str]:
        """Current credentials as MINIAPPS_* env vars (only non-empty ones)."""
        candidates = {
            "MINIAPPS_JWT": self._jwt,
            "MINIAPPS_CSRF_TOKEN": self._csrf_token,
            "MINIAPPS_CSRF_COOKIE": self._csrf_cookie,
            "MINIAPPS_CF_BM": self._cf_bm,
            "MINIAPPS_USER_EMAIL": self.user_email,
        }
        return {key: value for key, value in candidates.items() if value}


def _parse_body(response: requests.Response) -> Any:
    if not response.content:
        return None
    if "json" in response.headers.get("content-type", ""):
        try:
            return response.json()
        except ValueError:
            return response.text
    return response.text


def _iso(epoch: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None
