from pathlib import Path

from idea_mcp_proxy.summarize import render_summary
from idea_mcp_proxy.tree_parser import parse_tree

FIXTURES = Path(__file__).parent / "fixtures"


def test_render_summary_collapses_deep_test_leaves():
    raw = (FIXTURES / "analyze_calls_depth3.txt").read_text(encoding="utf-8")
    root = parse_tree(raw, root_signature="com.csc.conan.format.src.unmarshalling.fixedbyte.SRCByteReader.SRCByteReader")
    summary = render_summary(root)

    assert len(summary) < len(raw) * 0.1
    assert "Production call sites:" in summary
    assert "Test call sites:" in summary


def test_render_summary_collapses_flat_direct_fanout():
    # Regression: a symbol whose callers are mostly *direct* (one hop, no
    # children of their own) used to get every one of them listed
    # individually under "Direct:", defeating the whole point of
    # summarizing. Found via a real query where 43 of 51 total callers
    # were direct leaf callers of SRCByteReader.getInstance.
    raw = (FIXTURES / "analyze_calls_getinstance_flat_fanout.txt").read_text(encoding="utf-8")
    root = parse_tree(raw, root_signature="com.csc.conan.format.src.unmarshalling.fixedbyte.SRCByteReader.getInstance")
    summary = render_summary(root)

    assert len(summary) < len(raw) * 0.1

    # Direct leaf callers (no children of their own) must NOT be listed
    # individually -- only gateway nodes (direct callers that themselves
    # have further callers) get an individual line.
    assert "deltaJobShouldProduceExpectedOutput" not in summary
    assert "testCRecordSortingUsingTestfile" not in summary

    # Gateway nodes (have children) should still be named individually.
    assert "isSorted(Path)" in summary
    assert "runConfirmationAnalyze" in summary

    assert "Test call sites: 49 methods" in summary
