import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from idea_mcp_proxy.tool_filter import CODE_TOOLS, ToolFilter, UnknownPreset, parse_patterns


def test_default_preset_keeps_code_tools_and_drops_the_rest():
    f = ToolFilter.from_patterns(["@code"])
    for name in ("analyze_calls", "search_symbol", "rename_refactoring", "get_file_problems"):
        assert f.allows(name)
    for name in ("execute_terminal_command", "xdebug_set_breakpoint", "execute_sql_query",
                 "run_notebook_cell", "execute_run_configuration"):
        assert not f.allows(name)


def test_execute_tool_is_never_in_a_preset():
    # It invokes any tool by name, so exposing it would bypass the filter.
    assert "execute_tool" not in CODE_TOOLS
    assert not ToolFilter.from_patterns(["@code"]).allows("execute_tool")


def test_globs_exclude_and_star():
    f = ToolFilter.from_patterns(["search_*", "read_file"], ["search_regex"])
    assert f.allows("search_symbol") and f.allows("read_file")
    assert not f.allows("search_regex") and not f.allows("analyze_calls")
    assert ToolFilter.from_patterns(["*"]).allows("xdebug_get_stack")
    assert ToolFilter.from_patterns([]).allows("anything")


def test_unknown_preset_is_rejected():
    with pytest.raises(UnknownPreset):
        ToolFilter.from_patterns(["@nope"])


def test_parse_patterns():
    assert parse_patterns(" @code, search_* ,,") == ["@code", "search_*"]
    assert parse_patterns(None) == []


def _proxy(url: str, *extra: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", "idea_mcp_proxy.server", "--upstream", url, *extra]
    )


def test_hidden_tools_are_unlisted_and_refused(fake_ide):
    fake_ide.start()

    async def scenario():
        with anyio.fail_after(30):
            # Default preset: of the fake IDE's echo/slow/search_text, only
            # search_text is a code tool.
            async with stdio_client(_proxy(fake_ide.url)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert [t.name for t in (await session.list_tools()).tools] == ["search_text"]

                    result = await session.call_tool("echo", {"text": "x"})
                    assert result.is_error
                    assert "not exposed by idea-mcp-proxy" in result.content[0].text

            async with stdio_client(_proxy(fake_ide.url, "--include-tools", "echo,slow")) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = {t.name for t in (await session.list_tools()).tools}
                    assert names == {"echo", "slow"}
                    assert (await session.call_tool("echo", {"text": "x"})).content[0].text == "echo:x:gen1"

    anyio.run(scenario)


def test_bad_preset_fails_at_startup():
    from idea_mcp_proxy.server import main

    with pytest.raises(SystemExit) as exc:
        main(["--include-tools", "@nope"])
    assert exc.value.code == 2


def test_refusal_points_at_the_setting_that_hides_the_tool():
    f = ToolFilter.from_patterns(["@code"], ["apply_patch"])
    assert "--exclude-tools" in f.refusal("apply_patch")
    assert "--include-tools" in f.refusal("execute_terminal_command")
