"""Run the MiniApps tools as a local stdio MCP server.

For editors and desktop clients that spawn a process instead of connecting to
the hosted /mcp/sse endpoint:

    {
      "mcpServers": {
        "miniapps": {
          "command": "python",
          "args": ["mcp_stdio.py"],
          "env": { "MINIAPPS_JWT": "...", "MINIAPPS_CSRF_TOKEN": "...",
                   "MINIAPPS_CSRF_COOKIE": "...", "REQUIRE_API_KEY": "false" }
        }
      }
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from config import settings, users
from mcp_tools import TOOL_DEFS, dispatch
from proxy import MiniAppsProxy

# stdout is the MCP transport, so logs must go to stderr.
logging.basicConfig(level=settings.log_level.upper())
log = logging.getLogger("miniapps.mcp")

proxy = MiniAppsProxy(users[0])
server = Server(settings.mcp_server_name, version=settings.mcp_server_version)


@server.list_tools()
async def list_tools() -> List[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name=tool["name"],
            description=tool["description"],
            inputSchema=tool["inputSchema"],
        )
        for tool in TOOL_DEFS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
    result = await asyncio.to_thread(dispatch, proxy, name, arguments)
    return [
        mcp_types.TextContent(
            type="text", text=json.dumps(result, indent=2, ensure_ascii=False)
        )
    ]


async def _main() -> None:
    log.info("MiniApps MCP stdio server ready for %s", proxy.user_email or proxy.name)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
