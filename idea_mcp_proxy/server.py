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
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import mcp.types as types
from mcp import MCPError
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.stdio import stdio_server

from .analyze_calls_tool import WRAPPED_TOOL_NAME, build_wrapped_tool, handle_analyze_calls
from .tool_filter import (
    DEFAULT_INCLUDE,
    PRESETS,
    ToolFilter,
    UnknownPreset,
    parse_patterns,
)
from .compact import compact_tool
from .tool_descriptions import DISAMBIGUATION_NOTES, with_disambiguation_note
from .upstream import (
    DEFAULT_UPSTREAM_URL,
    UpstreamConnection,
    UpstreamUnavailable,
    connect as connect_upstream,
)

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
            yield upstream

    return _lifespan


def _accepts_project_path(t: types.Tool) -> bool:
    return "projectPath" in ((t.input_schema or {}).get("properties") or {})


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


def build_server(
    upstream_url: str = DEFAULT_UPSTREAM_URL,
    tool_filter: ToolFilter | None = None,
    project_path: str | None = None,
    compact: bool = True,
) -> Server:
    tool_filter = tool_filter or ToolFilter.from_patterns(DEFAULT_INCLUDE)
    # Tools whose schema accepts projectPath, learned from the upstream tool list.
    takes_project_path: set[str] = set()
    # Upstream connection generation the schemas were learned from; a
    # reconnect (IDE restart/upgrade) invalidates them.
    learned_generation: int | None = None

    def learn(upstream: UpstreamConnection, tools: list[types.Tool]) -> None:
        # Replace, don't accumulate: after an IDE upgrade a tool may no longer
        # accept projectPath, and the latest tools/list is the truth.
        nonlocal learned_generation
        takes_project_path.clear()
        takes_project_path.update(t.name for t in tools if _accepts_project_path(t))
        learned_generation = upstream.generation

    async def with_project_path(
        upstream: UpstreamConnection, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        """Fill in projectPath when the caller left it out.

        With several projects open, the IDE can't tell which one a call is for
        and fails it ("Unable to determine the target project"). Agents rarely
        pass projectPath unprompted, but the proxy knows the project: in stdio
        mode it runs in the directory the MCP client was started in.
        """
        # A non-empty projectPath from the caller wins. An empty one is treated
        # like a missing one on purpose: agents send "" when they don't know the
        # project, which is exactly the call the IDE can't resolve.
        if not project_path or arguments.get("projectPath"):
            return arguments
        if learned_generation is None or learned_generation != upstream.generation:
            # A call before any tools/list, or the first call on a new IDE
            # connection: learn the schemas (again). list_tools() (re)connects
            # first, so the generation is current once it returns.
            learn(upstream, (await upstream.list_tools()).tools)
        if name not in takes_project_path:
            return arguments
        return {**arguments, "projectPath": project_path}

    async def on_list_tools(
        ctx: ServerRequestContext[UpstreamConnection, object],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        upstream: UpstreamConnection = ctx.lifespan_context
        try:
            result = await upstream.list_tools()
        except UpstreamUnavailable as exc:
            # Surfaced to the MCP client as a JSON-RPC error with a readable message;
            # the proxy itself stays up, so a later reconnect (/mcp) picks the IDE up.
            raise MCPError(types.INTERNAL_ERROR, str(exc)) from exc
        learn(upstream, result.tools)
        tools = [
            build_wrapped_tool(t) if t.name == WRAPPED_TOOL_NAME else t
            for t in result.tools
            if tool_filter.allows(t.name)
        ]
        if compact:
            tools = [compact_tool(t, hide_project_path=bool(project_path)) for t in tools]
        else:
            tools = [_annotate_tool(t) for t in tools]
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(
        ctx: ServerRequestContext[UpstreamConnection, object],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult | types.InputRequiredResult:
        upstream: UpstreamConnection = ctx.lifespan_context
        arguments = params.arguments or {}

        if not tool_filter.allows(params.name):
            # Hidden tools are refused, not just unlisted: a client that
            # remembers or guesses a name must not get around the filter.
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=tool_filter.refusal(params.name))],
                is_error=True,
            )

        try:
            arguments = await with_project_path(upstream, params.name, arguments)
            if params.name == WRAPPED_TOOL_NAME:
                return await handle_analyze_calls(upstream, arguments)
            return await upstream.call_tool(params.name, arguments)
        except UpstreamUnavailable as exc:
            # A tool error (not a protocol error) so the agent sees the reason and
            # can tell the user to start the IDE instead of retrying blindly.
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))], is_error=True
            )

    return Server(
        "idea-mcp-proxy",
        version="0.1.0",
        instructions=(
            "Reverse proxy for the JetBrains IDE MCP server. Exposes the IDE's "
            "code-intelligence tools; analyze_calls is trimmed locally (see its "
            "description for the paths/sourceScope/mode params)."
        ),
        lifespan=_make_lifespan(upstream_url),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
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
        default=os.environ.get("IDEA_MCP_UPSTREAM_URL", DEFAULT_UPSTREAM_URL),
        help=(
            "URL of the JetBrains IDE MCP server "
            f"(default: $IDEA_MCP_UPSTREAM_URL or {DEFAULT_UPSTREAM_URL})"
        ),
    )
    parser.add_argument(
        "--include-tools",
        default=os.environ.get("IDEA_MCP_INCLUDE_TOOLS", ",".join(DEFAULT_INCLUDE)),
        help=(
            "comma-separated IDE tools to expose: names, globs (search_*) or presets "
            f"({', '.join('@' + k for k in PRESETS)}); '*' exposes everything "
            f"(default: $IDEA_MCP_INCLUDE_TOOLS or {','.join(DEFAULT_INCLUDE)})"
        ),
    )
    parser.add_argument(
        "--exclude-tools",
        default=os.environ.get("IDEA_MCP_EXCLUDE_TOOLS", ""),
        help="comma-separated tools to hide, same syntax; applied after --include-tools",
    )
    parser.add_argument(
        "--project-path",
        default=os.environ.get("IDEA_MCP_PROJECT_PATH"),
        help=(
            "IDE project to target when a tool call doesn't name one (needed when "
            "several projects are open). Default in stdio mode: the current directory, "
            "i.e. where the MCP client was started; '' disables it."
        ),
    )
    parser.add_argument(
        "--descriptions",
        choices=["compact", "original"],
        default=os.environ.get("IDEA_MCP_DESCRIPTIONS", "compact"),
        help=(
            "compact (default): short tool descriptions saying when to use the IDE and "
            "when Grep is enough, trimmed schemas; original: the IDE's own texts"
        ),
    )
    parser.add_argument(
        "--host",
        default=PROXY_HOST,
        choices=sorted(LOOPBACK_HOSTS),
        help="http transport only; loopback addresses only, the proxy is unauthenticated",
    )
    parser.add_argument("--port", type=int, default=PROXY_PORT, help="http transport only")
    args = parser.parse_args(argv)
    # argparse doesn't validate defaults, and this one can come from the environment.
    if args.descriptions not in ("compact", "original"):
        parser.error(f"invalid --descriptions / IDEA_MCP_DESCRIPTIONS value {args.descriptions!r}: use compact or original")

    # stderr only: in stdio mode stdout is the MCP protocol channel.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        tool_filter = ToolFilter.from_patterns(
            parse_patterns(args.include_tools), parse_patterns(args.exclude_tools)
        )
    except UnknownPreset as exc:
        parser.error(str(exc))
    project_path = args.project_path
    if project_path is None and args.transport == "stdio":
        project_path = os.getcwd()
    logger.info("default projectPath: %s", project_path or "(none)")
    server = build_server(
        args.upstream, tool_filter, project_path or None, compact=args.descriptions == "compact"
    )
    if args.transport == "stdio":
        anyio.run(_run_stdio, server)
    else:
        _run_http(server, args.host, args.port)


if __name__ == "__main__":
    main()
