"""Local stdio MCP server, for editors that spawn a process instead of using SSE.

Usage (e.g. in Claude Desktop / Cursor / Zed config):
    command: python
    args: ["/path/to/mcp_stdio.py"]
    env: { MINIAPPS_JWT: "...", MINIAPPS_CSRF_TOKEN: "...", MINIAPPS_CSRF_COOKIE: "...",
           REQUIRE_API_KEY: "false" }

The stdio transport has no HTTP layer and therefore no API key: it always acts
as the first configured user.
"""
from __future__ import annotations

import asyncio
import json

import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from config import settings, users
from mcp_tools import TOOL_DEFS, dispatch
from proxy import MiniAppsProxy

proxy = MiniAppsProxy(users[0])
server = Server(settings.mcp_server_name, version=settings.mcp_server_version)


@server.list_tools()
async def list_tools() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name=tool["name"],
            description=tool["description"],
            inputSchema=tool["inputSchema"],
        )
        for tool in TOOL_DEFS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[mcp_types.TextContent]:
    # `requests` is blocking, so keep it off the event loop.
    result = await asyncio.to_thread(dispatch, proxy, name, arguments)
    return [
        mcp_types.TextContent(
            type="text", text=json.dumps(result, indent=2, ensure_ascii=False)
        )
    ]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
