"""Configuration for the MiniApps Agent API.

Credentials can arrive three ways, in priority order:

1. ``MINIAPPS_USERS`` / ``MINIAPPS_USERS_FILE`` - a JSON array, for multi-tenant use.
2. ``MINIAPPS_CAPTURE`` / ``MINIAPPS_CAPTURE_FILE`` - a pasted DevTools request
   (cURL or Python ``requests``), parsed by :mod:`reseed`.
3. Individual ``MINIAPPS_JWT`` / ``MINIAPPS_CSRF_TOKEN`` / ``MINIAPPS_CSRF_COOKIE`` vars.
"""
from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

from reseed import parse_captured_request

log = logging.getLogger("miniapps.config")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

# Anything that still looks like it came from .env.example.
PLACEHOLDER_MARKERS = ("paste-", "your-", "replace-me", "changeme", "<", "example-value")


class UserConfig(BaseModel):
    """One miniapps.ai browser session, addressable by API key."""

    model_config = ConfigDict(extra="ignore")

    api_key: str
    name: str = "default"
    jwt: str
    csrf_token: str
    csrf_cookie: str
    cf_bm: str = ""
    user_id: str = ""
    user_email: str = ""


def reject_placeholder_values(user: UserConfig) -> UserConfig:
    """Fail fast when the example values were deployed unchanged."""
    offenders = [
        field
        for field in ("api_key", "jwt", "csrf_token", "csrf_cookie")
        if any(marker in (getattr(user, field) or "").lower() for marker in PLACEHOLDER_MARKERS)
    ]
    if offenders:
        raise RuntimeError(
            f"User {user.name!r} still uses placeholder values for: {', '.join(offenders)}. "
            "Copy a real authenticated request from DevTools and set the MINIAPPS_* vars."
        )
    return user


class Settings(BaseSettings):
    """Environment-driven settings. Reads a local .env when present."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Auth for *this* API (not for miniapps.ai)
    miniapps_api_key: str = ""
    require_api_key: bool = True

    # Single-user credentials
    miniapps_jwt: str = ""
    miniapps_csrf_token: str = ""
    miniapps_csrf_cookie: str = ""
    miniapps_cf_bm: str = ""
    miniapps_user_email: str = ""

    # Multi-user credentials
    miniapps_users: str = ""
    miniapps_users_file: str = ""

    # Seed straight from a captured request
    miniapps_capture: str = ""
    miniapps_capture_file: str = ""

    # Upstream
    miniapps_api_base: str = "https://api.miniapps.ai"
    miniapps_frontend_base: str = "https://miniapps.ai"
    miniapps_user_agent: str = DEFAULT_USER_AGENT
    request_timeout: float = 30.0

    # Server
    host: str = "0.0.0.0"
    port: int = 5000
    log_level: str = "info"

    # MCP
    mcp_server_name: str = "miniapps"
    mcp_server_version: str = "1.0.0"


def _parse_user_array(raw: str, source: str) -> List[UserConfig]:
    try:
        payload: Any = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"{source} is not valid JSON: {exc}") from exc
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"{source} must be a non-empty JSON array of user objects.")
    return [UserConfig(**entry) for entry in payload]


def _resolve_capture_key(settings: Settings) -> str:
    """API key for a capture-seeded user, generating one if needed."""
    if settings.miniapps_api_key:
        return settings.miniapps_api_key
    if not settings.require_api_key:
        return "local"
    generated = secrets.token_urlsafe(24)
    log.warning(
        "No MINIAPPS_API_KEY set. Generated a temporary key for this process: %s", generated
    )
    return generated


def load_users(settings: Settings) -> List[UserConfig]:
    """Build the user list from whichever configuration style is present."""
    if settings.miniapps_users.strip():
        users = _parse_user_array(settings.miniapps_users, "MINIAPPS_USERS")
    elif settings.miniapps_users_file.strip():
        path = Path(settings.miniapps_users_file)
        if not path.is_file():
            raise RuntimeError(f"MINIAPPS_USERS_FILE does not exist: {path}")
        users = _parse_user_array(path.read_text(encoding="utf-8"), str(path))
    else:
        users = [_single_user(settings)]

    keys = [user.api_key for user in users]
    if len(set(keys)) != len(keys):
        raise RuntimeError("Every configured user needs a unique api_key.")
    return [reject_placeholder_values(user) for user in users]


def _single_user(settings: Settings) -> UserConfig:
    """One user from explicit vars, falling back to a captured request."""
    values: Dict[str, str] = {
        "jwt": settings.miniapps_jwt.strip(),
        "csrf_token": settings.miniapps_csrf_token.strip(),
        "csrf_cookie": settings.miniapps_csrf_cookie.strip(),
        "cf_bm": settings.miniapps_cf_bm.strip(),
        "user_email": settings.miniapps_user_email.strip(),
    }

    capture = _read_capture(settings)
    if capture:
        parsed = parse_captured_request(capture)
        if parsed["_missing"]:
            raise RuntimeError(
                "Captured request is missing: "
                + ", ".join(parsed["_missing"])
                + ". Copy an authenticated api.miniapps.ai request from DevTools."
            )
        for field in ("jwt", "csrf_token", "csrf_cookie", "cf_bm", "user_id", "user_email"):
            if not values.get(field) and parsed.get(field):
                values[field] = str(parsed[field])
        if parsed.get("_expired"):
            log.warning(
                "The captured jwt already expired at %s. Calls will fail until you reseed.",
                parsed.get("expires_at"),
            )

    missing = [field for field in ("jwt", "csrf_token", "csrf_cookie") if not values.get(field)]
    if missing:
        raise RuntimeError(
            "Missing credentials: "
            + ", ".join(f"MINIAPPS_{field.upper()}" for field in missing)
            + ". Set them directly, or set MINIAPPS_CAPTURE/MINIAPPS_CAPTURE_FILE to a "
            "captured request, or MINIAPPS_USERS for multi-user mode."
        )

    return UserConfig(
        api_key=_resolve_capture_key(settings),
        name=values.get("user_email") or "default",
        **values,
    )


def _read_capture(settings: Settings) -> Optional[str]:
    if settings.miniapps_capture.strip():
        return settings.miniapps_capture
    if settings.miniapps_capture_file.strip():
        path = Path(settings.miniapps_capture_file)
        if not path.is_file():
            raise RuntimeError(f"MINIAPPS_CAPTURE_FILE does not exist: {path}")
        return path.read_text(encoding="utf-8")
    return None


settings = Settings()
users = load_users(settings)
