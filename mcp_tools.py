"""Shared MCP tool definitions, used by both the SSE server and stdio server."""
from __future__ import annotations

from typing import Any, Dict

from proxy import TOOL_KEYS, MiniAppsProxy

_TOOL_FLAGS = {
    key: {"type": "boolean", "description": f"Enable or disable {key}"} for key in TOOL_KEYS
}

TOOL_DEFS = [
    {
        "name": "get_tool_settings",
        "description": "Read the six tool toggles (webSearch, codeInterpreter, canvas, "
        "flightSearch, locationSearch, conversationLookup) for a miniapps.ai conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string", "description": "Conversation UUID"}},
            "required": ["conversation_id"],
        },
    },
    {
        "name": "set_tool_settings",
        "description": "Update tool toggles for a conversation. Only the flags you pass are "
        "changed; set merge=false to force every unlisted flag to false.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Conversation UUID"},
                **_TOOL_FLAGS,
                "merge": {
                    "type": "boolean",
                    "description": "Preserve unlisted flags (default true)",
                    "default": True,
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
            "properties": {"conversation_id": {"type": "string"}},
            "required": ["conversation_id"],
        },
    },
    {
        "name": "raw_request",
        "description": "Escape hatch: call any api.miniapps.ai path with the stored session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "default": "GET",
                },
                "path": {"type": "string", "description": "Upstream path, e.g. /conversations"},
                "body": {"type": "object", "description": "Optional JSON body"},
                "query": {"type": "object", "description": "Optional query parameters"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "session_status",
        "description": "Report the signed-in miniapps account, JWT expiry, and time remaining.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def dispatch(proxy: MiniAppsProxy, name: str, arguments: Dict[str, Any] | None) -> Any:
    """Run one MCP tool call against a proxy and return a JSON-serialisable result."""
    args = dict(arguments or {})

    if name == "get_tool_settings":
        conversation_id = args["conversation_id"]
        return {
            "conversationId": conversation_id,
            "toolSettings": proxy.get_tool_settings(conversation_id),
        }

    if name == "set_tool_settings":
        conversation_id = args.pop("conversation_id")
        merge = bool(args.pop("merge", True))
        desired = {key: bool(args[key]) for key in TOOL_KEYS if key in args}
        if not desired:
            raise ValueError("Pass at least one of: " + ", ".join(TOOL_KEYS))
        return proxy.set_tool_settings(conversation_id, desired, merge=merge)

    if name == "get_conversation":
        return proxy.get_conversation(args["conversation_id"])

    if name == "raw_request":
        status, body = proxy.request(
            args.get("method", "GET"),
            args["path"],
            json_body=args.get("body"),
            params=args.get("query"),
        )
        return {"status": status, "body": body}

    if name == "session_status":
        return proxy.status()

    raise ValueError(f"Unknown tool: {name}")
