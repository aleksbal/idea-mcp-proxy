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


def test_tree_cut_by_the_depth_limit_says_so():
    """Real IntelliJ output for MemberUtils.isMatchingMethod at the default depth:
    the invokeMethod overloads at the bottom have further callers (EventUtils),
    which the IDE marks with '+' but doesn't render. The proxy must not make that
    look like a complete tree."""
    from idea_mcp_proxy.summarize import filter_by_scope, render_summary, render_tree
    from idea_mcp_proxy.tree_parser import parse_tree

    raw = (FIXTURES / "analyze_calls_depth_cut.txt").read_text(encoding="utf-8")
    root = parse_tree(raw, root_signature="org.apache.commons.lang3.reflect.MemberUtils.isMatchingMethod(Method, Class<?>[])")
    tree = render_tree(filter_by_scope(root, "production"))
    assert "INCOMPLETE" in tree
    assert "org.apache.commons.lang3.reflect.MethodUtils.invokeMethod(Object, String)" in tree
    marked = [ln for ln in tree.splitlines() if "[+ more" in ln]
    assert any("invokeMethod(Object, String)  " in ln for ln in marked)
    # Real leaves (no '+' from the IDE) are not marked, and neither are nodes
    # whose only callers were tests removed by sourceScope.
    assert all("testNullArgument" not in ln for ln in marked)
    assert "INCOMPLETE" in render_summary(root)


def test_complete_tree_is_not_marked_incomplete():
    from idea_mcp_proxy.summarize import render_tree
    from idea_mcp_proxy.tree_parser import parse_tree

    raw = (FIXTURES / "analyze_calls_depth5.txt").read_text(encoding="utf-8")
    root = parse_tree(raw, root_signature="com.csc.conan.format.src.unmarshalling.fixedbyte.SRCByteReader.SRCByteReader(FileChannel, Charset)")
    text = render_tree(root)
    assert ("INCOMPLETE" in text) == any(ln for ln in text.splitlines() if "[+ more" in ln)


def test_incompleteness_survives_filters_that_remove_the_cut_nodes():
    from idea_mcp_proxy.summarize import filter_by_scope, render_summary, render_tree
    from idea_mcp_proxy.tree_parser import parse_tree

    raw = (FIXTURES / "analyze_calls_depth_cut.txt").read_text(encoding="utf-8")
    root = parse_tree(raw, root_signature="org.apache.commons.lang3.reflect.MemberUtils.isMatchingMethod(Method, Class<?>[])")
    # The cut nodes are production methods; sourceScope=test removes them, but
    # test callers beyond them may exist, so the result must still say so.
    only_tests = filter_by_scope(root, "test")
    assert "INCOMPLETE" in render_tree(only_tests, unfiltered=root)
    assert "INCOMPLETE" in render_summary(only_tests, unfiltered=root)
