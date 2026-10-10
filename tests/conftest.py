"""Shared fixtures: an in-process stand-in for the JetBrains IDE MCP server."""

from __future__ import annotations

import socket
import threading
import time

import anyio
import mcp.types as types
import pytest
import uvicorn
from mcp.server.lowlevel import Server


def _fake_ide_server(generation: int) -> Server:
    async def on_list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="echo",
                    description="echoes its input",
                    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                ),
                types.Tool(
                    name="slow",
                    description="takes a few seconds",
                    input_schema={"type": "object", "properties": {}},
                ),
                types.Tool(
                    name="search_text",
                    description="text search",
                    input_schema={"type": "object", "properties": {}},
                ),
            ]
        )

    async def on_call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name == "slow":
            await anyio.sleep(5)
        text = (params.arguments or {}).get("text", "")
        # The generation shows which IDE "process" answered, so tests can
        # tell a reconnect to a restarted IDE apart from a stale connection.
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"echo:{text}:gen{generation}")]
        )

    return Server("fake-ide", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeIde:
    """Fake IDE MCP server on a fixed port that can be stopped and restarted."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/stream"
        self.generation = 0
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.generation += 1
        app = _fake_ide_server(self.generation).streamable_http_app(
            streamable_http_path="/stream", host="127.0.0.1"
        )
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake IDE server did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.should_exit = True
        self._server.force_exit = True
        self._thread.join(timeout=10)
        self._server = None


@pytest.fixture
def fake_ide():
    ide = FakeIde()
    yield ide
    ide.stop()
