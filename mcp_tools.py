"""MCP tool definitions, shared by the SSE server in app.py and mcp_stdio.py.

Keeping the schemas and dispatch in one module means the HTTP transport and the
stdio transport can never drift apart.
"""
from __future__ import annotations

from typing import Any, Dict

from proxy import TOOL_KEYS, MiniAppsProxy

_TOOL_FLAGS: Dict[str, Any] = {
    key: {"type": "boolean", "description": f"Enable or disable the {key} tool."}
    for key in TOOL_KEYS
}

TOOL_DEFS = [
    {
        "name": "get_tool_settings",
        "description": "Read the current tool toggles for a miniapps.ai conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Conversation UUID."}
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "set_tool_settings",
        "description": (
            "Enable or disable tools for a conversation. Only the flags you pass are "
            "changed; set merge=false to force every unlisted flag to false, which is "
            "what the browser PUT does."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Conversation UUID."},
                **_TOOL_FLAGS,
                "merge": {
                    "type": "boolean",
                    "default": True,
                    "description": "Keep flags that were not passed. Defaults to true.",
                },
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "get_conversation",
        "description": "Fetch the full conversation object from miniapps.ai.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Conversation UUID."}
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "raw_request",
        "description": (
            "Call any api.miniapps.ai endpoint with the stored browser session. "
            "Use this for endpoints this server does not model yet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "default": "GET",
                },
                "path": {
                    "type": "string",
                    "description": "Upstream path, e.g. /conversations/<uuid>.",
                },
                "body": {"type": "object", "description": "Optional JSON body."},
                "params": {"type": "object", "description": "Optional query parameters."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "session_status",
        "description": (
            "Report the stored miniapps.ai session: user, issue/expiry times, and "
            "whether the jwt needs reseeding."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def dispatch(proxy: MiniAppsProxy, name: str, arguments: Dict[str, Any]) -> Any:
    """Run one MCP tool call against a proxy and return a JSON-safe result."""
    args = arguments or {}

    if name == "get_tool_settings":
        conversation_id = _require(args, "conversation_id")
        return {
            "conversationId": conversation_id,
            "toolSettings": proxy.get_tool_settings(conversation_id),
        }

    if name == "set_tool_settings":
        conversation_id = _require(args, "conversation_id")
        desired = {key: bool(args[key]) for key in TOOL_KEYS if key in args}
        if not desired:
            raise ValueError("Pass at least one of: " + ", ".join(TOOL_KEYS))
        return proxy.set_tool_settings(
            conversation_id, desired, merge=bool(args.get("merge", True))
        )

    if name == "get_conversation":
        return proxy.get_conversation(_require(args, "conversation_id"))

    if name == "raw_request":
        status, body = proxy.request(
            str(args.get("method", "GET")),
            _require(args, "path"),
            json_body=args.get("body"),
            params=args.get("params"),
        )
        return {"status": status, "body": body}

    if name == "session_status":
        return proxy.status()

    raise ValueError(
        f"Unknown tool {name!r}. Available: " + ", ".join(tool["name"] for tool in TOOL_DEFS)
    )


def _require(args: Dict[str, Any], field: str) -> str:
    value = args.get(field)
    if not value or not isinstance(value, str):
        raise ValueError(f"{field} is required and must be a string.")
    return value
