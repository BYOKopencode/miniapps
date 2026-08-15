"""MiniApps Agent API - a REST + MCP front door for miniapps.ai conversations."""
from __future__ import annotations

import logging
import re
import secrets
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

import requests as rq
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import settings, users
from proxy import TOOL_KEYS, MiniAppsProxy, SessionExpired
from reseed import parse_captured_request

logging.basicConfig(level=settings.log_level.upper())
log = logging.getLogger("miniapps")

app = FastAPI(
    title="MiniApps Agent API",
    version=settings.mcp_server_version,
    description="REST + MCP wrapper around the miniapps.ai conversation API.",
)

# One long-lived proxy per configured user, keyed by API key.
proxies: Dict[str, MiniAppsProxy] = {user.api_key: MiniAppsProxy(user) for user in users}
default_proxy: MiniAppsProxy = next(iter(proxies.values()))

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_RESEED_FIELDS = ("jwt", "csrf_token", "csrf_cookie", "cf_bm", "user_id", "user_email")


# ── Auth ──────────────────────────────────────────────────────────

def _extract_api_key(request: Request) -> Optional[str]:
    """Accept either `Authorization: Bearer <key>` or `X-API-Key: <key>`."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.headers.get("x-api-key") or None


def _resolve_proxy(api_key: Optional[str]) -> MiniAppsProxy:
    if not settings.require_api_key:
        return proxies.get(api_key or "", default_proxy)
    if not api_key:
        raise HTTPException(401, "Missing API key. Send Authorization: Bearer <key> or X-API-Key.")
    # Compare against every key so timing does not leak which one matched.
    matched: Optional[MiniAppsProxy] = None
    for key, proxy in proxies.items():
        if secrets.compare_digest(api_key, key):
            matched = proxy
    if matched is None:
        raise HTTPException(401, "Invalid API key.")
    return matched


def require_user(request: Request) -> MiniAppsProxy:
    """Dependency: resolve the caller's API key to its miniapps session."""
    return _resolve_proxy(_extract_api_key(request))


@app.exception_handler(StarletteHTTPException)
async def openai_style_http_errors(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Return errors in the shape agent frameworks already know how to read."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.detail,
                "type": "invalid_request_error" if exc.status_code < 500 else "api_error",
                "code": exc.status_code,
            }
        },
    )


@contextmanager
def upstream_errors() -> Iterator[None]:
    """Map upstream/session failures onto meaningful HTTP responses."""
    try:
        yield
    except SessionExpired as exc:
        raise HTTPException(401, str(exc)) from exc
    except rq.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else 502
        detail = (response.text or "")[:2000] if response is not None else str(exc)
        if status in (401, 403):
            detail = (
                f"{detail} | miniapps.ai rejected the stored session. The `jwt` cookie or the "
                "CSRF pair is stale - POST a fresh capture to /auth/reseed."
            ).strip(" |")
        raise HTTPException(status, detail or f"Upstream returned {status}") from exc
    except rq.RequestException as exc:
        raise HTTPException(502, f"Could not reach miniapps.ai: {exc}") from exc


def valid_cid(conversation_id: str) -> str:
    if not UUID_RE.match(conversation_id):
        raise HTTPException(422, f"conversation_id must be a UUID, got {conversation_id!r}")
    return conversation_id


# ── Models ──────────────────────────────────────────────────────

class ToolSettingsPatch(BaseModel):
    """The six toggles from the miniapps conversation settings panel."""

    model_config = ConfigDict(extra="forbid")

    webSearch: Optional[bool] = None
    codeInterpreter: Optional[bool] = None
    canvas: Optional[bool] = None
    flightSearch: Optional[bool] = None
    locationSearch: Optional[bool] = None
    conversationLookup: Optional[bool] = None

    def desired(self) -> Dict[str, bool]:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class CookieUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jwt: Optional[str] = None
    csrf_token: Optional[str] = None
    csrf_cookie: Optional[str] = None
    cf_bm: Optional[str] = None


class ReseedRequest(BaseModel):
    """Either paste a captured request in `raw`, or set the fields explicitly."""

    model_config = ConfigDict(extra="forbid")

    raw: Optional[str] = None
    jwt: Optional[str] = None
    csrf_token: Optional[str] = None
    csrf_cookie: Optional[str] = None
    cf_bm: Optional[str] = None
    user_id: Optional[str] = None
    user_email: Optional[str] = None


# ── Health ──────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health() -> Dict[str, Any]:
    """Public. Used by the Railway healthcheck, so it must not require a key."""
    return {
        "status": "ok",
        "configured_users": len(proxies),
        "require_api_key": settings.require_api_key,
        "upstream": settings.miniapps_api_base,
        "sessions": [
            {
                "name": proxy.name,
                "user_email": proxy.user_email,
                "jwt_expires_at": proxy.status()["jwt_expires_at"],
                "jwt_expired": proxy.status()["jwt_expired"],
            }
            for proxy in proxies.values()
        ],
    }


# ── Conversations ──────────────────────────────────────────────────

@app.get("/v1/conversations/{conversation_id}", tags=["conversations"])
def get_conversation(
    conversation_id: str, proxy: MiniAppsProxy = Depends(require_user)
) -> Any:
    with upstream_errors():
        return proxy.get_conversation(valid_cid(conversation_id))


@app.get("/v1/conversations/{conversation_id}/tool-settings", tags=["conversations"])
def read_tool_settings(
    conversation_id: str, proxy: MiniAppsProxy = Depends(require_user)
) -> Dict[str, Any]:
    with upstream_errors():
        return {
            "conversationId": valid_cid(conversation_id),
            "toolSettings": proxy.get_tool_settings(conversation_id),
        }


@app.put("/v1/conversations/{conversation_id}/tool-settings", tags=["conversations"])
@app.patch("/v1/conversations/{conversation_id}/tool-settings", tags=["conversations"])
def write_tool_settings(
    conversation_id: str,
    patch: ToolSettingsPatch,
    merge: bool = Query(
        True,
        description="Keep flags you did not send. false reproduces the raw browser PUT, "
        "which sets every unlisted flag to false.",
    ),
    proxy: MiniAppsProxy = Depends(require_user),
) -> Dict[str, Any]:
    desired = patch.desired()
    if not desired:
        raise HTTPException(422, "Send at least one of: " + ", ".join(TOOL_KEYS))
    with upstream_errors():
        return proxy.set_tool_settings(valid_cid(conversation_id), desired, merge=merge)


@app.api_route(
    "/v1/raw/{upstream_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    tags=["raw"],
)
async def raw_passthrough(
    upstream_path: str, request: Request, proxy: MiniAppsProxy = Depends(require_user)
) -> Any:
    """Escape hatch for any api.miniapps.ai endpoint not modelled above."""
    body: Any = None
    if request.method in ("POST", "PUT", "PATCH"):
        raw = await request.body()
        if raw:
            try:
                import json

                body = json.loads(raw)
            except ValueError as exc:
                raise HTTPException(400, f"Body must be JSON: {exc}") from exc
    with upstream_errors():
        status, payload = proxy.request(
            request.method,
            f"/{upstream_path}",
            json_body=body,
            params=dict(request.query_params),
        )
    return JSONResponse(status_code=status, content={"status": status, "body": payload})


# ── Session management ─────────────────────────────────────────────

@app.get("/auth/status", tags=["auth"])
def auth_status(proxy: MiniAppsProxy = Depends(require_user)) -> Dict[str, Any]:
    return proxy.status()


@app.post("/auth/cookies", tags=["auth"])
def update_cookies(
    payload: CookieUpdateRequest, proxy: MiniAppsProxy = Depends(require_user)
) -> Dict[str, Any]:
    """Hot-swap individual credentials without a redeploy."""
    values = payload.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(422, "Send at least one of: jwt, csrf_token, csrf_cookie, cf_bm.")
    proxy.update_cookies(**values)
    return {"updated": sorted(values), "status": proxy.status()}


@app.post("/auth/reseed", tags=["auth"])
def reseed(
    payload: ReseedRequest, proxy: MiniAppsProxy = Depends(require_user)
) -> Dict[str, Any]:
    """Reseed from a pasted DevTools capture (cURL or Python requests)."""
    values = {
        field: getattr(payload, field)
        for field in _RESEED_FIELDS
        if getattr(payload, field) is not None
    }
    parsed_meta: Dict[str, Any] = {}
    if payload.raw:
        parsed = parse_captured_request(payload.raw)
        if parsed["_missing"]:
            raise HTTPException(
                422,
                "Capture is missing required fields: "
                + ", ".join(parsed["_missing"])
                + ". Copy an authenticated api.miniapps.ai request from DevTools.",
            )
        for field in _RESEED_FIELDS:
            if parsed.get(field) and field not in values:
                values[field] = parsed[field]
        parsed_meta = {
            "issued_at": parsed.get("issued_at"),
            "expires_at": parsed.get("expires_at"),
            "missing_optional": parsed.get("_missing_optional"),
            "expired": parsed.get("_expired"),
        }
    if not values:
        raise HTTPException(422, "Send `raw` with a captured request, or explicit credentials.")
    try:
        proxy.reseed(**values)
    except SessionExpired as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("Reseeded session for %s", proxy.user_email or proxy.name)
    return {"reseeded": sorted(values), "capture": parsed_meta, "status": proxy.status()}


@app.get("/auth/env", tags=["auth"])
def auth_env(proxy: MiniAppsProxy = Depends(require_user)) -> Dict[str, str]:
    """Current credentials as MINIAPPS_* vars, to paste back into deploy config."""
    return proxy.export_env()


# ── MCP (SSE) ─────────────────────────────────────────────────────
# Exposes the same operations as MCP tools at /mcp/sse so editors and agent
# runtimes can attach directly. MCP has no per-request API key, so these tools
# always act as the first configured user.

try:  # pragma: no cover - optional dependency
    import json as _json

    import mcp.types as mcp_types
    from mcp.server.lowlevel import Server as MCPServer
    from mcp.server.sse import SseServerTransport
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    from mcp_tools import TOOL_DEFS, dispatch

    mcp_server = MCPServer(settings.mcp_server_name, version=settings.mcp_server_version)

    @mcp_server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name=tool["name"],
                description=tool["description"],
                inputSchema=tool["inputSchema"],
            )
            for tool in TOOL_DEFS
        ]

    @mcp_server.call_tool()
    async def _call_tool(name: str, arguments: Dict[str, Any]) -> list[mcp_types.TextContent]:
        # `requests` is blocking, so keep it off the event loop.
        result = await run_in_threadpool(dispatch, default_proxy, name, arguments)
        return [
            mcp_types.TextContent(
                type="text", text=_json.dumps(result, indent=2, ensure_ascii=False)
            )
        ]

    _sse = SseServerTransport("/mcp/messages/")

    async def _handle_sse(request: Request) -> Response:
        async with _sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream, write_stream, mcp_server.create_initialization_options()
            )
        return Response(status_code=204)

    app.router.routes.append(Route("/mcp/sse", endpoint=_handle_sse, methods=["GET"]))
    app.router.routes.append(Mount("/mcp/messages/", app=_sse.handle_post_message))
    log.info("MCP SSE server mounted at /mcp/sse")
except ImportError as exc:  # pragma: no cover
    log.warning("MCP support disabled (%s). REST endpoints are unaffected.", exc)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app, host=settings.host, port=settings.port, log_level=settings.log_level
    )
