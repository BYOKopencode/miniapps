"""Environment-driven configuration and multi-user credential loading."""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class UserConfig(BaseModel):
    """One API consumer and its independent miniapps.ai browser identity."""

    model_config = ConfigDict(extra="ignore")

    api_key: str = Field(min_length=1)
    name: str | None = None

    # Credentials copied out of a signed-in miniapps.ai session.
    jwt: str = Field(min_length=1)          # `jwt` cookie
    csrf_token: str = Field(min_length=1)   # `x-csrf-token` request header (long value)
    csrf_cookie: str = Field(min_length=1)  # `__Host-miniapps.x-csrf-token` cookie (short value)

    # Optional: Cloudflare bot-management cookie. It lives ~30 minutes and the
    # proxy keeps whatever Cloudflare hands back, so seeding it is never required.
    cf_bm: str = ""

    # Informational only; both are derived from the JWT when omitted.
    user_id: str = ""
    user_email: str = ""

    @field_validator("*", mode="after")
    @classmethod
    def reject_placeholder_values(cls, value: str | None, info) -> str | None:
        """Catch unfilled placeholders early.

        Credentials are sent as HTTP header and cookie values, which must be
        latin-1 encodable. A copy-pasted placeholder such as `<jwt — see .env>`
        would otherwise fail deep inside the request with an opaque codec error.
        """
        if not isinstance(value, str):
            return value
        try:
            value.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"{info.field_name} contains a non-latin-1 character "
                f"({exc.object[exc.start]!r}); it looks like an unreplaced placeholder "
                "rather than a real credential"
            ) from exc
        if value.startswith("<") and value.endswith(">"):
            raise ValueError(
                f"{info.field_name} is still the placeholder {value!r}; "
                "replace it with the real value"
            )
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Authentication / multi-user sources
    require_api_key: bool = True
    miniapps_users_file: str = "users.json"
    miniapps_users: str | None = None

    # Captured browser request (cURL / python-requests dump)
    miniapps_capture_file: str = "capture.txt"
    miniapps_capture: str | None = None

    # Legacy single-user configuration
    miniapps_api_key: str | None = None
    miniapps_jwt: str | None = None
    miniapps_csrf_token: str | None = None
    miniapps_csrf_cookie: str | None = None
    miniapps_cf_bm: str | None = None
    miniapps_user_email: str | None = None

    # Endpoints / upstream behaviour
    miniapps_api_base: str = "https://api.miniapps.ai"
    miniapps_frontend_base: str = "https://miniapps.ai"
    miniapps_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    )
    request_timeout: float = 60.0

    # Server / MCP
    host: str = "0.0.0.0"
    port: int = 5000
    log_level: str = "info"
    mcp_server_name: str = "miniapps-agent"
    mcp_server_version: str = "1.0.0"

    def _parse_user_array(self, value: Any, source: str) -> list[UserConfig]:
        try:
            raw = json.loads(value) if isinstance(value, str) else value
            if not isinstance(raw, list):
                raise ValueError("must be a JSON array")
            return [UserConfig.model_validate(item) for item in raw]
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            raise RuntimeError(f"Invalid user configuration in {source}: {exc}") from exc

    def load_users(self) -> list[UserConfig]:
        """Merge file, inline JSON, capture, and legacy sources; later sources win by key."""
        merged: dict[str, UserConfig] = {}

        users_path = Path(self.miniapps_users_file)
        if users_path.is_file():
            for user in self._parse_user_array(users_path.read_text("utf-8"), str(users_path)):
                merged[user.api_key] = user

        if self.miniapps_users:
            for user in self._parse_user_array(self.miniapps_users, "MINIAPPS_USERS"):
                merged[user.api_key] = user

        # Captured browser request -> credentials (highest-priority single user).
        capture_text = self.miniapps_capture
        if not capture_text:
            capture_path = Path(self.miniapps_capture_file)
            if capture_path.is_file():
                capture_text = capture_path.read_text("utf-8")
        if capture_text:
            from reseed import parse_captured_request

            parsed = parse_captured_request(capture_text)
            if parsed["_missing"]:
                raise RuntimeError(
                    "Captured request is missing required fields: "
                    + ", ".join(parsed["_missing"])
                    + ". Re-copy the request from DevTools (Copy as cURL / as Python requests)."
                )
            merged_key = self._resolve_capture_key()
            captured = UserConfig.model_validate({
                "api_key": merged_key,
                "name": "captured",
                "jwt": parsed["jwt"],
                "csrf_token": parsed["csrf_token"],
                "csrf_cookie": parsed["csrf_cookie"],
                "cf_bm": parsed.get("cf_bm") or "",
                "user_id": parsed.get("user_id") or "",
                "user_email": self.miniapps_user_email or parsed.get("user_email") or "",
            })
            merged[captured.api_key] = captured

        # `api_key` is handled separately: the credential fields below decide
        # whether legacy single-user mode is in use at all.
        legacy_values = {
            "jwt": self.miniapps_jwt,
            "csrf_token": self.miniapps_csrf_token,
            "csrf_cookie": self.miniapps_csrf_cookie,
        }
        if any(value is not None for value in legacy_values.values()) and not capture_text:
            missing = [key for key, value in legacy_values.items() if value is None]
            if missing:
                raise RuntimeError(
                    "Incomplete legacy user configuration; missing: " + ", ".join(missing)
                )
            legacy = UserConfig.model_validate({
                **legacy_values,
                "api_key": self._resolve_capture_key("legacy"),
                "name": "legacy",
                "cf_bm": self.miniapps_cf_bm or "",
                "user_email": self.miniapps_user_email or "",
            })
            merged[legacy.api_key] = legacy

        if not merged:
            raise RuntimeError(
                "No miniapps users configured. Add capture.txt, users.json, MINIAPPS_USERS, "
                "or the legacy MINIAPPS_* variables."
            )
        return list(merged.values())

    def _resolve_capture_key(self, mode: str = "captured") -> str:
        """API key for single-user sources, or a random one when auth is disabled."""
        if self.miniapps_api_key:
            return self.miniapps_api_key
        if self.require_api_key:
            raise RuntimeError(
                f"{mode.capitalize()} credentials found but MINIAPPS_API_KEY is not set. "
                "Add MINIAPPS_API_KEY=<your-key> to authenticate requests, or set "
                "REQUIRE_API_KEY=false for local single-user development."
            )
        # Auth disabled: any key is accepted, so a random internal key is fine.
        return f"local-{secrets.token_urlsafe(16)}"


settings = Settings()
users = settings.load_users()
