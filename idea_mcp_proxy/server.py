"""Reverse proxy MCP server.

Sits between an MCP client (e.g. Claude Code) and the real JetBrains IDE MCP
server. Every tool is passed through unchanged except `analyze_calls`, whose
verbose output is trimmed locally before being returned -- see
analyze_calls_tool.py for why and how.

Run directly:
    .venv/Scripts/python.exe -m idea_mcp_proxy.server
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext

from .analyze_calls_tool import WRAPPED_TOOL_NAME, build_wrapped_tool, handle_analyze_calls
from .upstream import DEFAULT_UPSTREAM_URL, UpstreamConnection, connect as connect_upstream

logger = logging.getLogger("idea_mcp_proxy")

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8765
PROXY_PATH = "/stream"


@asynccontextmanager
async def _lifespan(_server: Server) -> AsyncIterator[UpstreamConnection]:
    async with connect_upstream(DEFAULT_UPSTREAM_URL) as upstream:
        logger.info("connected to upstream IDE server at %s", DEFAULT_UPSTREAM_URL)
        yield upstream


async def _on_list_tools(
    ctx: ServerRequestContext[UpstreamConnection, object],
    _params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    upstream: UpstreamConnection = ctx.lifespan_context
    result = await upstream.session.list_tools()
    tools = [
        build_wrapped_tool(t) if t.name == WRAPPED_TOOL_NAME else t
        for t in result.tools
    ]
    return types.ListToolsResult(tools=tools)


async def _on_call_tool(
    ctx: ServerRequestContext[UpstreamConnection, object],
    params: types.CallToolRequestParams,
) -> types.CallToolResult | types.InputRequiredResult:
    upstream: UpstreamConnection = ctx.lifespan_context
    arguments = params.arguments or {}

    if params.name == WRAPPED_TOOL_NAME:
        return await handle_analyze_calls(upstream, arguments)

    return await upstream.session.call_tool(params.name, arguments)


def build_server() -> Server:
    return Server(
        "idea-mcp-proxy",
        version="0.1.0",
        instructions=(
            "Reverse proxy for the JetBrains IDE MCP server. All tools are "
            "passed through unchanged except analyze_calls, which is trimmed "
            "locally (see its description for the paths/sourceScope/mode params)."
        ),
        lifespan=_lifespan,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = build_server()
    app = server.streamable_http_app(streamable_http_path=PROXY_PATH, host=PROXY_HOST)
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="info")


if __name__ == "__main__":
    main()
