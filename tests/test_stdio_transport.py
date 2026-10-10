"""End-to-end: MCP client -> proxy over stdio -> fake IDE server over HTTP.

Spawns the proxy exactly the way Claude Code would (as a child process
speaking MCP on stdin/stdout) and points it at an in-process stand-in for
the JetBrains server, so no running IDE is needed.
"""

from __future__ import annotations

import socket
import sys
import threading
import time

import anyio
import mcp.types as types
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server


def _fake_ide_server() -> Server:
    async def on_list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="echo",
                    description="echoes its input",
                    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                ),
                types.Tool(
                    name="search_text",
                    description="text search",
                    input_schema={"type": "object", "properties": {}},
                ),
            ]
        )

    async def on_call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        text = (params.arguments or {}).get("text", "")
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"echo:{text}")])

    return Server("fake-ide", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_ide_url():
    port = _free_port()
    app = _fake_ide_server().streamable_http_app(streamable_http_path="/stream", host="127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("fake IDE server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/stream"
    server.should_exit = True
    thread.join(timeout=5)


def test_stdio_proxy_lists_and_passes_through_tools(fake_ide_url):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "idea_mcp_proxy.server", "--transport", "stdio", "--upstream", fake_ide_url],
    )

    async def scenario():
        with anyio.fail_after(30):
            await _exercise(params)

    anyio.run(scenario)


async def _exercise(params: StdioServerParameters) -> None:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert set(tools) == {"echo", "search_text"}
            assert tools["echo"].description == "echoes its input"
            # Disambiguation note is still applied in stdio mode.
            assert "[disambiguation]" in tools["search_text"].description

            result = await session.call_tool("echo", {"text": "hi"})
            assert result.content[0].text == "echo:hi"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_http_mode_rejects_non_loopback_host(host):
    from idea_mcp_proxy.server import main

    with pytest.raises(SystemExit) as exc:
        main(["--transport", "http", "--host", host])
    assert exc.value.code == 2
