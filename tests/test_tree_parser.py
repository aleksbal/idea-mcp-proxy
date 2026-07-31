from pathlib import Path

import pytest

from idea_mcp_proxy.tree_parser import TreeParseError, parse_tree

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_depth3_sample():
    raw = (FIXTURES / "analyze_calls_depth3.txt").read_text(encoding="utf-8")
    root = parse_tree(raw, root_signature="com.csc.conan.format.src.unmarshalling.fixedbyte.SRCByteReader.SRCByteReader")
    assert root.name == "SRCByteReader(FileChannel, Charset)"
    assert root.count_nodes() == 52


def test_parses_names_and_treepaths_containing_brackets():
    # Regression: Java array/generic syntax like "main(String[])" contains
    # literal '[' / ']' that previously broke the bracket-exclusion regex.
    raw = (FIXTURES / "analyze_calls_depth5.txt").read_text(encoding="utf-8")
    root = parse_tree(raw, root_signature="com.csc.conan.format.src.unmarshalling.fixedbyte.SRCByteReader.SRCByteReader")

    found = []

    def walk(n):
        found.append(n)
        for c in n.children:
            walk(c)

    walk(root)

    main_node = next(n for n in found if n.name == "main(String[])")
    assert main_node.signature == "com.dxc.kontenabruf.kwgbackend.shared.util.IkdCliTool.main(String[])"

    dispatch_node = next(n for n in found if n.name.startswith("dispatch("))
    assert "IkdCliTool.dispatch(String, Map<String, String>)" in dispatch_node.signature


def test_empty_input_raises():
    with pytest.raises(TreeParseError):
        parse_tree("", root_signature="x.Y.z()")


def test_garbage_input_raises():
    with pytest.raises(TreeParseError):
        parse_tree("this is not a tree at all\njust text", root_signature="x.Y.z()")
