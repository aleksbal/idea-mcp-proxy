import mcp.types as types

from idea_mcp_proxy.compact import DESCRIPTIONS, compact_tool


def _tool(name="analyze_calls"):
    return types.Tool(
        name=name,
        description="A very long description. With many sentences. " * 20,
        input_schema={
            "type": "object",
            "properties": {
                "symbolFqn": {"type": "string", "description": "Long text. " * 30},
                "projectPath": {"type": "string", "description": "ASK the USER ... " * 10},
                "timeout": {"type": "integer", "description": "Timeout in milliseconds"},
                "depth": {"type": "integer", "description": "First sentence. Second sentence."},
            },
            "required": ["symbolFqn", "projectPath"],
        },
    )


def test_compact_replaces_description_and_hides_proxy_filled_params():
    t = compact_tool(_tool(), hide_project_path=True)
    assert t.description == DESCRIPTIONS["analyze_calls"]
    assert set(t.input_schema["properties"]) == {"symbolFqn", "depth"}
    assert t.input_schema["required"] == ["symbolFqn"]
    assert len(t.input_schema["properties"]["symbolFqn"]["description"]) < 80


def test_project_path_stays_visible_when_the_proxy_does_not_fill_it():
    t = compact_tool(_tool(), hide_project_path=False)
    assert "projectPath" in t.input_schema["properties"]
    assert t.input_schema["required"] == ["symbolFqn", "projectPath"]


def test_unknown_tools_get_their_first_sentence():
    t = compact_tool(_tool("some_new_tool"), hide_project_path=True)
    assert t.description == "A very long description."
    assert t.input_schema["properties"]["depth"]["description"] == "First sentence."


def test_semantic_tools_all_have_a_routing_description():
    from idea_mcp_proxy.tool_filter import SEMANTIC_TOOLS

    assert set(SEMANTIC_TOOLS) <= set(DESCRIPTIONS)


def test_first_sentence_does_not_stop_at_abbreviations():
    from idea_mcp_proxy.compact import _first_sentence

    assert _first_sentence("Filters files, e.g. ['a']. Applied locally.") == "Filters files, e.g. ['a']."


def test_compact_keeps_other_protocol_fields():
    t = _tool().model_copy(update={"annotations": types.ToolAnnotations(read_only_hint=True)})
    assert compact_tool(t, hide_project_path=True).annotations.read_only_hint is True


def test_invalid_descriptions_env_value_fails_at_startup(monkeypatch):
    import pytest
    from idea_mcp_proxy.server import main

    monkeypatch.setenv("IDEA_MCP_DESCRIPTIONS", "compactt")
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_routing_rules_are_sent_as_server_instructions(fake_ide):
    import sys

    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    fake_ide.start()

    async def instructions(*extra):
        params = StdioServerParameters(command=sys.executable, args=["-m", "idea_mcp_proxy.server", "--upstream", fake_ide.url, *extra])
        with anyio.fail_after(30):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    return (await session.initialize()).instructions

    async def scenario():
        full = await instructions()
        assert "rename_refactoring" in full and "build_project" in full and "analyze_calls" in full
        # Read-only preset: no rules pointing at tools the proxy would refuse.
        read_only = await instructions("--include-tools", "@semantic-read")
        assert "rename_refactoring" not in read_only and "build_project" not in read_only
        assert "analyze_calls" in read_only and "Grep" in read_only

    anyio.run(scenario)


def test_each_routing_rule_names_only_exposed_tools():
    from idea_mcp_proxy.compact import ROUTING_RULES, routing_instructions

    exposed = {"lint_files", "get_project_dependencies"}
    text = routing_instructions(lambda name: name in exposed)
    all_tools = {tool for _, (tool,) in ROUTING_RULES}
    for tool in all_tools - exposed:
        assert tool not in text, tool
    assert "lint_files" in text and "get_project_dependencies" in text
