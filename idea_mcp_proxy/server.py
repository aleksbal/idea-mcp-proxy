"""Reverse proxy MCP server.

Sits between an MCP client (e.g. Claude Code) and the real JetBrains IDE MCP
server. Every tool is passed through unchanged except `analyze_calls`, whose
verbose output is trimmed locally before being returned -- see
analyze_calls_tool.py for why and how.

Run directly:
    idea-mcp-proxy                     # stdio (launched by the MCP client)
    idea-mcp-proxy --transport http    # standalone on 127.0.0.1:8765/stream
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.stdio import stdio_server

from .analyze_calls_tool import WRAPPED_TOOL_NAME, build_wrapped_tool, handle_analyze_calls
from .tool_descriptions import DISAMBIGUATION_NOTES, with_disambiguation_note
from .upstream import DEFAULT_UPSTREAM_URL, UpstreamConnection, connect as connect_upstream

logger = logging.getLogger("idea_mcp_proxy")

PROXY_HOST = "127.0.0.1"
# The proxy has no authentication and passes through IDE tools that can run
# commands and edit files, so it must never listen beyond this machine.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PROXY_PORT = 8765
PROXY_PATH = "/stream"


def _make_lifespan(upstream_url: str):
    @asynccontextmanager
    async def _lifespan(_server: Server) -> AsyncIterator[UpstreamConnection]:
        async with connect_upstream(upstream_url) as upstream:
            logger.info("connected to upstream IDE server at %s", upstream_url)
            yield upstream

    return _lifespan


def _annotate_tool(t: types.Tool) -> types.Tool:
    """Appends a disambiguation note to an otherwise-unmodified tool.

    Only touches `description` -- schema, name, and behavior are untouched,
    this is purely a discoverability aid for picking between near-duplicate
    tools (the search_* family especially).
    """
    if t.name not in DISAMBIGUATION_NOTES:
        return t
    return types.Tool(
        name=t.name,
        title=t.title,
        description=with_disambiguation_note(t.description, t.name),
        input_schema=t.input_schema,
        output_schema=t.output_schema,
    )


async def _on_list_tools(
    ctx: ServerRequestContext[UpstreamConnection, object],
    _params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    upstream: UpstreamConnection = ctx.lifespan_context
    result = await upstream.session.list_tools()
    tools = [
        build_wrapped_tool(t) if t.name == WRAPPED_TOOL_NAME else _annotate_tool(t)
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


def build_server(upstream_url: str = DEFAULT_UPSTREAM_URL) -> Server:
    return Server(
        "idea-mcp-proxy",
        version="0.1.0",
        instructions=(
            "Reverse proxy for the JetBrains IDE MCP server. All tools are "
            "passed through unchanged except analyze_calls, which is trimmed "
            "locally (see its description for the paths/sourceScope/mode params)."
        ),
        lifespan=_make_lifespan(upstream_url),
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


async def _run_stdio(server: Server) -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _run_http(server: Server, host: str, port: int) -> None:
    import uvicorn

    app = server.streamable_http_app(streamable_http_path=PROXY_PATH, host=host)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="idea-mcp-proxy", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help=(
            "stdio (default): the MCP client launches the proxy as a child process. "
            "http: run as a standalone server on --host/--port."
        ),
    )
    parser.add_argument(
        "--upstream",
        default=DEFAULT_UPSTREAM_URL,
        help=f"URL of the JetBrains IDE MCP server (default: {DEFAULT_UPSTREAM_URL})",
    )
    parser.add_argument(
        "--host",
        default=PROXY_HOST,
        choices=sorted(LOOPBACK_HOSTS),
        help="http transport only; loopback addresses only, the proxy is unauthenticated",
    )
    parser.add_argument("--port", type=int, default=PROXY_PORT, help="http transport only")
    args = parser.parse_args(argv)

    # stderr only: in stdio mode stdout is the MCP protocol channel.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    server = build_server(args.upstream)
    if args.transport == "stdio":
        anyio.run(_run_stdio, server)
    else:
        _run_http(server, args.host, args.port)


if __name__ == "__main__":
    main()
