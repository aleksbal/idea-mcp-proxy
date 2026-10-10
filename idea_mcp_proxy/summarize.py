"""
Turns a parsed CallNode tree into compact output for an LLM caller.

This is where the actual token savings happen: the raw tool repeats each
node's full ancestor treePath as a string, and by default renders every test
caller as its own line even when dozens share an identical one-hop shape.
Here we:

  1. classify each leaf's file as production or test source,
  2. optionally drop test-sourced branches entirely (sourceScope filter),
  3. optionally collapse structurally-identical sibling leaves into a single
     grouped summary line instead of one line each.

Default Maven/Gradle-style test detection: a path containing a `/test/`
(or `\\test\\`) segment, or a filename ending in Test/Tests/IT/Spec before
the extension. This is a heuristic, not a guarantee -- projects with
non-standard layouts should override via `test_path_pattern`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .tree_parser import CallNode

DEFAULT_TEST_PATTERN = re.compile(
    r"[/\\]test[/\\]|(Test|Tests|IT|Spec)\.java$|(Test|Tests|IT|Spec)\.kt$",
    re.IGNORECASE,
)

SourceScope = Literal["production", "test", "all"]
Mode = Literal["summary", "tree"]


@dataclass
class SummaryStats:
    direct_callers: int
    total_callers: int
    production_count: int
    test_count: int


def _is_test_node(node: CallNode, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.search(node.file_path))


def filter_by_scope(
    root: CallNode,
    scope: SourceScope,
    test_path_pattern: re.Pattern[str] = DEFAULT_TEST_PATTERN,
) -> CallNode:
    """Returns a new tree with branches outside the requested scope pruned.

    A node is kept if it itself matches the scope, OR any descendant does
    (so production call chains that happen to pass through, e.g., a
    same-named helper aren't accidentally dropped). Leaf classification
    is purely by file path of that leaf.
    """
    if scope == "all":
        return root

    def prune(node: CallNode) -> CallNode | None:
        kept_children = [c for c in (prune(child) for child in node.children) if c is not None]
        is_test = _is_test_node(node, test_path_pattern)
        node_matches = (scope == "test") == is_test
        if not kept_children and not node_matches and node.children:
            # had children but none survived, and this node itself doesn't match
            return None
        if not node.children and not node_matches:
            return None
        new_node = CallNode(
            name=node.name,
            file_path=node.file_path,
            signature=node.signature,
            depth=node.depth,
            usages=node.usages,
            expandable=node.expandable,
            cut=node.cut,
            truncated_children=node.truncated_children,
        )
        new_node.children = kept_children
        return new_node

    pruned = prune(root)
    if pruned is None:
        # Root itself never matches sourceScope (it's the queried symbol,
        # not a caller) -- keep it with empty children rather than None.
        empty = CallNode(
            name=root.name,
            file_path=root.file_path,
            signature=root.signature,
            depth=root.depth,
            usages=root.usages,
            expandable=root.expandable,
        )
        return empty
    return pruned


def filter_by_paths(root: CallNode, include: list[str] | None, exclude: list[str] | None) -> CallNode:
    """Glob-style path filtering, mirroring search_symbol's `paths` param.

    Kept minimal (fnmatch) rather than pulling in a globbing dependency;
    sufficient for the `!**/test/**`-style patterns this is meant for.
    """
    import fnmatch

    def matches(path: str, patterns: list[str]) -> bool:
        normalized = path.replace("\\", "/")
        return any(fnmatch.fnmatch(normalized, p.lstrip("!")) for p in patterns)

    def keep(path: str) -> bool:
        if include and not matches(path, include):
            return False
        if exclude and matches(path, exclude):
            return False
        return True

    def prune(node: CallNode) -> CallNode | None:
        kept_children = [c for c in (prune(child) for child in node.children) if c is not None]
        if not node.children:
            return node if keep(node.file_path) else None
        if not kept_children:
            return None
        new_node = CallNode(
            name=node.name, file_path=node.file_path, signature=node.signature,
            depth=node.depth, usages=node.usages, expandable=node.expandable,
            cut=node.cut, truncated_children=node.truncated_children,
        )
        new_node.children = kept_children
        return new_node

    pruned = prune(root)
    return pruned if pruned is not None else root


def compute_stats(root: CallNode, test_path_pattern: re.Pattern[str] = DEFAULT_TEST_PATTERN) -> SummaryStats:
    all_nodes: list[CallNode] = []

    def walk(n: CallNode) -> None:
        all_nodes.append(n)
        for c in n.children:
            walk(c)

    walk(root)
    non_root = all_nodes[1:]
    leaves = [n for n in non_root if not n.children]
    prod = sum(1 for n in leaves if not _is_test_node(n, test_path_pattern))
    test = sum(1 for n in leaves if _is_test_node(n, test_path_pattern))
    return SummaryStats(
        direct_callers=len(root.children),
        total_callers=len(leaves),
        production_count=prod,
        test_count=test,
    )


def render_tree(root: CallNode, max_depth: int | None = None) -> str:
    """Compact re-serialization: nesting via indentation, treePath NOT
    repeated per line (that's the redundancy this whole thing exists to
    remove) -- ancestry is implicit from indentation, as in a normal tree.
    """
    lines: list[str] = []

    def walk(node: CallNode, indent: int) -> None:
        if max_depth is not None and indent > max_depth:
            return
        usage_note = f" [{node.usages} usages]" if node.usages > 1 else ""
        cut_note = "  [+ more callers, not shown: depth limit]" if node.cut else ""
        lines.append("  " * indent + f"- {node.name}{usage_note}  ({node.file_path}){cut_note}")
        for c in node.children:
            walk(c, indent + 1)

    walk(root, 0)
    return "\n".join(lines) + _cut_footer(root)


def _cut_nodes(root: CallNode) -> list[CallNode]:
    found: list[CallNode] = []

    def walk(n: CallNode) -> None:
        if n.cut:
            found.append(n)
        for c in n.children:
            walk(c)

    walk(root)
    return found


def _cut_footer(root: CallNode, limit: int = 10) -> str:
    """Say explicitly when the tree is incomplete, and how to get the rest.

    Without this, a tree cut by the depth limit looks complete: an agent asking
    for all (indirect) callers would take the deepest rendered callers for the
    last ones.
    """
    cut = _cut_nodes(root)
    if not cut:
        return ""
    sigs = [n.signature for n in cut[:limit]]
    more = f" (+{len(cut) - limit} more)" if len(cut) > limit else ""
    return (
        f"\n\nINCOMPLETE: {len(cut)} branch(es) cut at the depth limit -- these have further "
        f"callers not shown{more}. Call analyze_calls again with a larger `depth`, or with "
        f"symbolFqn set to one of: " + "; ".join(sigs)
    )


def render_summary(
    root: CallNode,
    test_path_pattern: re.Pattern[str] = DEFAULT_TEST_PATTERN,
    collapse_threshold: int = 4,
) -> str:
    """Grouped summary: direct callers that themselves have further callers
    ("gateway" nodes -- worth naming individually since they represent a
    branch) are listed one per line. Every leaf -- whether it's several
    hops deep or a direct caller with no further callers of its own -- is
    grouped by (containing file, production/test) instead of getting its
    own line. Without this split, a symbol with a large *flat* fan-out
    (many things call it directly, e.g. a widely-used factory method) would
    list every direct caller individually and defeat the point of
    summarizing at all -- this was found by testing against a real query
    where 43 of 51 total callers were direct.
    """
    stats = compute_stats(root, test_path_pattern)
    lines = [
        f"Direct callers: {stats.direct_callers}",
        f"Total callers found: {stats.total_callers} "
        f"({stats.production_count} production, {stats.test_count} test)",
    ]

    gateway_children = [c for c in root.children if c.children]
    if gateway_children:
        lines.append("")
        lines.append("Direct (with further callers of their own):")
        for child in gateway_children:
            lines.append(f"  - {child.name}  ({child.file_path})")

    # Every leaf, whether several hops deep or a direct caller with no
    # children of its own, grouped by file and production/test.
    leaves: list[CallNode] = []

    def collect_leaves(n: CallNode) -> None:
        if not n.children:
            leaves.append(n)
        for c in n.children:
            collect_leaves(c)

    collect_leaves(root)
    by_file: dict[str, list[CallNode]] = {}
    for leaf in leaves:
        by_file.setdefault(leaf.file_path, []).append(leaf)

    prod_files = {f: ns for f, ns in by_file.items() if not _is_test_node(ns[0], test_path_pattern)}
    test_files = {f: ns for f, ns in by_file.items() if _is_test_node(ns[0], test_path_pattern)}

    if prod_files:
        lines.append("\nProduction call sites:")
        for f, nodes in prod_files.items():
            names = ", ".join(n.name for n in nodes)
            lines.append(f"  - {f}: {names}")

    if test_files:
        total_test_methods = sum(len(ns) for ns in test_files.values())
        if len(test_files) > collapse_threshold:
            lines.append(
                f"\nTest call sites: {total_test_methods} methods across {len(test_files)} test files "
                f"(pass sourceScope='test' for the full list)"
            )
        else:
            lines.append("\nTest call sites:")
            for f, nodes in test_files.items():
                names = ", ".join(n.name for n in nodes)
                lines.append(f"  - {f}: {names}")

    return "\n".join(lines) + _cut_footer(root)
