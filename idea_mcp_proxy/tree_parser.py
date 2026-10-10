"""
Parser for the raw text tree returned by the real IDE server's `analyze_calls` tool.

Ground truth sample (captured via scripts/probe_real_server.py against
IntelliJ IDEA MCP Server 2026.2.0.1) looks like:

    └─ + SRCByteReader(FileChannel, Charset) [filePath="...", treePath=[]]
       └─ + createReader(Path, Charset) [filePath="...", treePath=["com...createReader(Path, Charset)"]]
          ├─ + getAsciiInstance(Path) [filePath="...", treePath=["com...createReader(Path, Charset)","com...getAsciiInstance(Path)"]]
          │  └─ testFoo() [filePath="...", treePath=["com...createReader(Path, Charset)","com...getAsciiInstance(Path)","com...testFoo()"]]
          ...

Key insight: `treePath` is the full ancestor signature chain *including the
node's own signature as the last element*. That means tree structure can be
reconstructed purely from treePath prefix relationships -- no need to parse
the indentation or box-drawing connectors (`├─`, `└─`, `│`) at all. Those are
kept only for reference; a node's parent is simply the node whose own
treePath equals this node's treePath[:-1].

The root node is the queried symbol itself. It always has treePath=[] and no
signature is exposed for it in the raw text, so it's synthesized from the
symbolFqn used in the request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# One line, e.g.:
#   │  └─ + getEbcdicInstance(Path) [2 usages, filePath="a\\b.java", treePath=["x.y()","a.b()"]]
#
# Note: `name` and treePath signatures can themselves contain literal '[' / ']'
# (Java array/generic syntax, e.g. "main(String[])"), so we can't exclude
# brackets to find the boundary -- instead we anchor on the fixed literal
# `[filePath="` marker (non-greedy `name`) and consume everything up to the
# final `]]` on the line (greedy `tree_path`), which is safe because that
# literal marker and the trailing `]]` can each only occur once in that role.
#
# Group breakdown:
#   name       -> display name, e.g. "getEbcdicInstance(Path)"
#   usages     -> optional leading "N usages" annotation
#   file_path  -> raw filePath string (still containing escaped backslashes)
#   tree_path  -> raw contents of the treePath=[...] array (comma-separated quoted strings)
_LINE_RE = re.compile(
    r"""^[\s│]*[├└]─\s+          # indent + connector (ignored beyond stripping)
        (?P<expandable>\+\s+)?    # optional "+ " expandable marker
        (?P<name>.+?)\s*
        \[
        (?:(?P<usages>\d+)\s+usages,\s*)?
        filePath="(?P<file_path>(?:[^"\\]|\\.)*)"
        ,\s*treePath=\[(?P<tree_path>.*)\]
        \]\s*$
    """,
    re.VERBOSE,
)

# Individual quoted signature inside a treePath=[...] array.
_TREE_PATH_ITEM_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')

# The "... and N more" truncation marker mentioned in analyze_calls' tool
# description (appears when a node's children exceed maxChildren).
_TRUNCATION_RE = re.compile(r"^\s*[│\s]*\.\.\.\s+and\s+(\d+)\s+more")


def _unescape(s: str) -> str:
    return s.replace('\\\\', '\\').replace('\\"', '"')


@dataclass
class CallNode:
    """A single node in the reconstructed call hierarchy tree."""

    name: str
    file_path: str
    signature: str  # fully qualified, e.g. "com.example.Foo.bar(String)"
    depth: int
    usages: int = 1
    expandable: bool = False
    # Has callers of its own (the IDE's "+") but none were rendered: the tree
    # was cut here by a render limit (depth, maxNodes or maxChildren). Set once at parse time, so a node whose
    # children are later removed by a filter is not mistaken for a cut one.
    cut: bool = False
    truncated_children: int = 0  # count from a "... and N more" marker, if any
    children: list["CallNode"] = field(default_factory=list)

    def is_test(self, test_path_pattern: re.Pattern[str]) -> bool:
        return bool(test_path_pattern.search(self.file_path))

    def count_nodes(self) -> int:
        return 1 + sum(c.count_nodes() for c in self.children)


class TreeParseError(ValueError):
    """Raised when the raw text doesn't match the expected grammar.

    Callers should catch this and fall back to returning the raw text
    unmodified rather than surfacing a broken/partial tree.
    """


def parse_tree(raw_text: str, root_signature: str, root_name: str | None = None) -> CallNode:
    """Parse analyze_calls' raw rendered text into a CallNode tree.

    root_signature should be the symbolFqn (or exact signature) passed in the
    original request -- the root line's own treePath is always `[]`, so its
    signature has to come from the request rather than the text.
    """
    lines = [ln for ln in raw_text.splitlines() if ln.strip()]
    if not lines:
        raise TreeParseError("empty analyze_calls response")

    root: CallNode | None = None
    # signature (tuple of tree_path) -> node, for O(1) parent lookup
    by_path: dict[tuple[str, ...], CallNode] = {}

    for line_no, line in enumerate(lines, start=1):
        trunc = _TRUNCATION_RE.match(line)
        if trunc:
            # Attach to the most recently seen node's parent -- best effort;
            # exact attachment isn't critical since callers re-request via
            # childOffset explicitly when they want more.
            continue

        m = _LINE_RE.match(line)
        if not m:
            raise TreeParseError(f"line {line_no} did not match expected grammar: {line!r}")

        name = m.group("name").strip()
        file_path = _unescape(m.group("file_path"))
        usages = int(m.group("usages")) if m.group("usages") else 1
        expandable = m.group("expandable") is not None

        tree_path_raw = m.group("tree_path")
        tree_path = tuple(_unescape(x) for x in _TREE_PATH_ITEM_RE.findall(tree_path_raw))

        if not tree_path:
            # Root node: treePath=[]
            root = CallNode(
                name=root_name or name,
                file_path=file_path,
                signature=root_signature,
                depth=0,
                usages=usages,
                expandable=expandable,
            )
            by_path[()] = root
            continue

        signature = tree_path[-1]
        parent_path = tree_path[:-1]
        parent = by_path.get(parent_path)
        if parent is None:
            raise TreeParseError(
                f"line {line_no}: parent path {parent_path!r} not seen before this node "
                f"(out-of-order or unsupported response shape)"
            )

        node = CallNode(
            name=name,
            file_path=file_path,
            signature=signature,
            depth=parent.depth + 1,
            usages=usages,
            expandable=expandable,
        )
        parent.children.append(node)
        by_path[tree_path] = node

    if root is None:
        raise TreeParseError("no root node (treePath=[]) found in response")

    for node in by_path.values():
        node.cut = node.expandable and not node.children
    return root
