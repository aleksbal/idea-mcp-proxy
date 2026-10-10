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
