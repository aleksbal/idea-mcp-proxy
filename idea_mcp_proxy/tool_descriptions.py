"""
Description enrichment for tools that are otherwise passed through unchanged.

The real server's tool descriptions are mostly accurate but leave several
near-duplicate tools (the search_* family especially) to be disambiguated by
trial and error. Since the proxy already controls what `on_list_tools`
returns, it can append a short cross-reference note to the existing
description -- no schema or behavior change, just better discoverability at
the moment an agent is choosing between tools.

Keep these targeted at genuine ambiguity, not uniform padding: padding every
tool's description adds noise to keyword-based tool search without helping
anyone pick correctly.
"""

from __future__ import annotations

DISAMBIGUATION_NOTES: dict[str, str] = {
    "search_file": (
        "Matches file paths only -- it does not look inside file contents. "
        "For finding code by name or text, use `search_symbol` (semantic "
        "identifier lookup) or `search_text`/`search_regex` (raw content "
        "search) instead."
    ),
    "search_text": (
        "Matches literal text anywhere in file contents -- including "
        "comments, strings, and unrelated same-named identifiers, so "
        "results can include false positives. For finding a specific "
        "class/method/field by name, prefer `search_symbol` instead: it's "
        "index-based and won't match inside comments/strings. Use this "
        "tool when the target isn't a code symbol (e.g. a config key, a "
        "log message) or when `search_symbol` finds nothing."
    ),
    "search_regex": (
        "Same content-matching caveats as `search_text` -- no semantic "
        "awareness, can match inside comments/strings. Use only when the "
        "query genuinely needs a regex pattern (e.g. a family of "
        "similarly-shaped strings) rather than a single symbol name; for a "
        "symbol name, prefer `search_symbol`."
    ),
    "search_symbol": (
        "This is the preferred starting point for \"where is X defined\" "
        "questions about code identifiers -- index-based, so it correctly "
        "distinguishes real declarations from incidental text matches. Its "
        "results feed directly into `analyze_calls` (call graph) and "
        "`get_symbol_info` (declaration detail). Use `search_text`/"
        "`search_regex` instead only when the target isn't a symbol at all."
    ),
    "get_symbol_info": (
        "Answers \"what is this symbol\" for one specific position in one "
        "file -- it is not a search tool. To locate that position from a "
        "name fragment first, use `search_symbol`. For \"who calls this\" "
        "or \"what does this call\", use `analyze_calls` instead."
    ),
    "analyze_calls": (
        "For the symbol's own declaration or documentation, rather than "
        "its callers, use `get_symbol_info` instead."
    ),
    "rename_refactoring": (
        "This is a write operation with project-wide side effects, unlike "
        "the read-only search/analysis tools above. If `symbolName` could "
        "be ambiguous, confirm it first via `search_symbol` or "
        "`get_symbol_info` -- a wrong match renames an unintended "
        "identifier."
    ),
    "generate_psi_tree": (
        "Narrower than it sounds: this is for inspecting PSI node "
        "structure while authoring an inspection "
        "(`run_inspection_kts`/`generate_inspection_kts_api`), not a "
        "general \"show me the AST\" tool for everyday exploration. For "
        "ordinary \"what does this identifier resolve to\" questions, use "
        "`get_symbol_info` or `search_symbol` instead -- cheaper, and "
        "already do the semantic resolution this would otherwise require "
        "manual AST-reading to replicate."
    ),
}


def with_disambiguation_note(description: str | None, tool_name: str) -> str | None:
    note = DISAMBIGUATION_NOTES.get(tool_name)
    if note is None:
        return description
    base = description or ""
    return f"{base}\n\n[disambiguation] {note}"
