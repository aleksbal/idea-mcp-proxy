"""End-to-end: MCP client -> proxy over stdio -> fake IDE server over HTTP.

Spawns the proxy exactly the way Claude Code would (as a child process
speaking MCP on stdin/stdout) and points it at an in-process stand-in for
the JetBrains server (see conftest.py), so no running IDE is needed.
"""

from __future__ import annotations

import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _proxy_params(upstream_url: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "idea_mcp_proxy.server", "--transport", "stdio", "--upstream", upstream_url, "--include-tools", "*"],
    )


def test_stdio_proxy_lists_and_passes_through_tools(fake_ide):
    fake_ide.start()

    async def scenario():
        with anyio.fail_after(30):
            async with stdio_client(_proxy_params(fake_ide.url)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    tools = {t.name: t for t in (await session.list_tools()).tools}
                    assert set(tools) == {"echo", "slow", "where", "search_text"}
                    assert tools["echo"].description == "echoes its input"
                    # Disambiguation note is still applied in stdio mode.
                    assert "[disambiguation]" in tools["search_text"].description

                    result = await session.call_tool("echo", {"text": "hi"})
                    assert result.content[0].text == "echo:hi:gen1"

    anyio.run(scenario)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_http_mode_rejects_non_loopback_host(host):
    from idea_mcp_proxy.server import main

    with pytest.raises(SystemExit) as exc:
        main(["--transport", "http", "--host", host])
    assert exc.value.code == 2
