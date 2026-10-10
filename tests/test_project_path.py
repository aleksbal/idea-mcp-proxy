"""The proxy fills in projectPath so calls work with several IDE projects open."""

from __future__ import annotations

import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _calls(url: str, extra: list[str], calls: list[tuple[str, dict]], cwd: str | None = None, list_first=True):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "idea_mcp_proxy.server", "--upstream", url, "--include-tools", "*", *extra],
        cwd=cwd,
    )
    out = []
    with anyio.fail_after(30):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if list_first:
                    await session.list_tools()
                for name, args in calls:
                    out.append((await session.call_tool(name, args)).content[0].text)
    return out


def test_defaults_to_the_directory_the_client_started_in(fake_ide, tmp_path):
    fake_ide.start()
    got = anyio.run(_calls, fake_ide.url, [], [("where", {})], str(tmp_path))
    assert got == [f"project:{tmp_path}"]


def test_explicit_option_caller_value_and_tools_without_the_param(fake_ide):
    fake_ide.start()
    got = anyio.run(
        _calls, fake_ide.url, ["--project-path", "/proj"],
        [("where", {}), ("where", {"projectPath": "/mine"}), ("echo", {"text": "x"})],
    )
    # Caller's own value wins; a tool whose schema lacks projectPath gets none
    # (the fake echo tool would ignore it anyway, but it must not error).
    assert got == ["project:/proj", "project:/mine", "echo:x:gen1"]


def test_works_for_a_call_before_any_tools_list(fake_ide):
    fake_ide.start()
    got = anyio.run(_calls, fake_ide.url, ["--project-path", "/proj"], [("where", {})], None, False)
    assert got == ["project:/proj"]


def test_empty_value_disables_it(fake_ide):
    fake_ide.start()
    got = anyio.run(_calls, fake_ide.url, ["--project-path", ""], [("where", {})])
    assert got == ["project:<none>"]
