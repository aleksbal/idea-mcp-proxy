from idea_mcp_proxy.tool_descriptions import DISAMBIGUATION_NOTES, with_disambiguation_note


def test_appends_note_for_known_tool():
    result = with_disambiguation_note("Original description.", "search_symbol")
    assert result.startswith("Original description.")
    assert "[disambiguation]" in result
    assert DISAMBIGUATION_NOTES["search_symbol"] in result


def test_passthrough_for_unknown_tool():
    result = with_disambiguation_note("Original description.", "git_status")
    assert result == "Original description."


def test_handles_none_description():
    result = with_disambiguation_note(None, "search_file")
    assert result.startswith("\n\n[disambiguation]")
