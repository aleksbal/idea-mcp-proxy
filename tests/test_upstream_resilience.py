"""The proxy must survive the IDE being down or restarting (issue #6).

Each test runs the real proxy over stdio against the fake IDE from
conftest.py, which can be stopped and restarted on the same port.
"""

from __future__ import annotations

import sys

import anyio
import pytest
from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _with_proxy(upstream_url: str, body) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "idea_mcp_proxy.server", "--upstream", upstream_url],
    )
    with anyio.fail_after(60):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await body(session)


def _text(result) -> str:
    return result.content[0].text


def test_proxy_starts_while_ide_is_down_and_explains_why(fake_ide):
    async def body(session: ClientSession):
        # initialize already succeeded with the IDE down -- the proxy no
        # longer needs the IDE at startup.
        with pytest.raises(MCPError, match="not reachable at"):
            await session.list_tools()

        result = await session.call_tool("echo", {"text": "x"})
        assert result.is_error
        assert "not reachable at" in _text(result)
        assert "MCP Server enabled" in _text(result)

    anyio.run(_with_proxy, fake_ide.url, body)


def test_ide_started_after_proxy_is_picked_up(fake_ide):
    async def body(session: ClientSession):
        result = await session.call_tool("echo", {"text": "x"})
        assert result.is_error

        fake_ide.start()

        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == {"echo", "slow", "search_text"}
        assert _text(await session.call_tool("echo", {"text": "x"})) == "echo:x:gen1"

    anyio.run(_with_proxy, fake_ide.url, body)


def test_ide_restart_mid_session_reconnects_transparently(fake_ide):
    fake_ide.start()

    async def body(session: ClientSession):
        assert _text(await session.call_tool("echo", {"text": "a"})) == "echo:a:gen1"

        fake_ide.stop()
        fake_ide.start()

        # The connection from before the restart is stale; the proxy must
        # notice (ping) and reconnect before sending the call.
        assert _text(await session.call_tool("echo", {"text": "b"})) == "echo:b:gen2"

    anyio.run(_with_proxy, fake_ide.url, body)


def test_ide_stopped_mid_session_returns_clear_error_then_recovers(fake_ide):
    fake_ide.start()

    async def body(session: ClientSession):
        assert _text(await session.call_tool("echo", {"text": "a"})) == "echo:a:gen1"

        fake_ide.stop()
        result = await session.call_tool("echo", {"text": "b"})
        assert result.is_error
        assert "not reachable at" in _text(result)

        fake_ide.start()
        assert _text(await session.call_tool("echo", {"text": "c"})) == "echo:c:gen2"

    anyio.run(_with_proxy, fake_ide.url, body)


def test_ide_dying_during_a_call_is_reported_not_retried(fake_ide):
    fake_ide.start()

    async def body(session: ClientSession):
        result = None

        async def call():
            nonlocal result
            result = await session.call_tool("slow", {})

        async with anyio.create_task_group() as tg:
            tg.start_soon(call)
            await anyio.sleep(1)
            await anyio.to_thread.run_sync(fake_ide.stop)

        # The IDE may have executed the tool, so the proxy must not silently
        # re-run it -- it reports the lost call instead.
        assert result.is_error
        assert "lost during the call" in _text(result)

    anyio.run(_with_proxy, fake_ide.url, body)
