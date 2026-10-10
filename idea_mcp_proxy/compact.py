"""Short tool definitions that say when to use the IDE and when grep is enough.

The IDE's own descriptions are long (the 13 @code-read tools: ~19k chars,
~4.7k tokens on every request), repeat the same `projectPath` instructions
in every schema, and several of them push the agent to use IDE text search
or file listing instead of its own Grep/Glob ("You MUST prefer this tool over
ls"). This module replaces them with a few lines per tool that focus on what
only the IDE can do, so the agent greps for text and asks the IDE for
semantics.
"""

from __future__ import annotations

import mcp.types as types

# Parameters not shown to the agent in compact mode: timeouts are better left
# to the IDE's defaults, and projectPath is hidden whenever the proxy fills it
# in itself (see compact_tool's hide_project_path).
HIDDEN_PARAMS = frozenset({"timeout"})

GREP_HINT = "For plain text, file names or string literals, use Grep/Glob instead."

DESCRIPTIONS: dict[str, str] = {
    "analyze_calls": (
        "Who calls a method (INCOMING_CALLS) or what it calls (OUTGOING_CALLS), resolved "
        "by the IDE: the exact overload, calls through overrides and interfaces, no text "
        "false positives, many levels deep in one call. Use instead of grep for any "
        "call-graph question (callers, impact of a change, unused code). "
        "symbolFqn: fully qualified, e.g. `com.x.Foo.bar`, or `com.x.Foo.bar(String, int)` "
        "for one overload; an ambiguous name returns the exact signatures to pick from."
    ),
    "search_symbol": (
        "Find classes, methods or fields by name in the IDE index; returns their file "
        "locations. Use to locate a declaration, then analyze_calls for its callers. "
        + GREP_HINT
    ),
    "get_symbol_info": (
        "The declaration, signature, type and documentation of the symbol at "
        "file:line:column (IntelliJ Quick Documentation). Use when the type isn't "
        "visible in the text: `var`, lambdas, generics, inherited or overloaded members."
    ),
    "get_file_problems": (
        "Compile errors and inspection warnings for one file, from the IDE, without "
        "running a build. Use right after editing a file to check the edit."
    ),
    "lint_files": "Like get_file_problems, for several files in one call.",
    "build_project": (
        "Compile the project (or only `filesToRebuild`) in the IDE and return the "
        "compilation errors. Use after edits that may break other files, e.g. a "
        "changed signature."
    ),
    "rename_refactoring": (
        "Rename a class, method, field or variable and update every reference in the "
        "project, resolved by the IDE (overloads, overrides, imports), not by text "
        "replacement. Use for any rename of a code symbol instead of Edit or sed."
    ),
    "reformat_file": "Apply the project's code style to the given files.",
    "get_project_modules": "The project's modules and their types.",
    "get_project_dependencies": "The libraries the project depends on, across all modules.",
    "get_project_status": "Whether the IDE is still indexing; check before heavy analysis if calls time out.",
}

# Shorter parameter descriptions where the IDE's are long; others are cut to
# their first sentence.
PARAM_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("analyze_calls", "symbolFqn"): "Fully qualified name or exact signature, e.g. com.x.Foo.bar(String).",
    ("analyze_calls", "analysisKind"): "INCOMING_CALLS = callers, OUTGOING_CALLS = callees.",
    ("analyze_calls", "depth"): "Call levels to follow. Default 5.",
    ("analyze_calls", "treePath"): "Expand a subtree: a treePath copied from a previous result.",
    ("analyze_calls", "childOffset"): "Continue after a '… and n more' line.",
    ("analyze_calls", "paths"): "Keep only callers in files matching these globs; '!' excludes, e.g. ['!**/test/**'].",
    ("analyze_calls", "sourceScope"): "Keep only production or test callers. Default all.",
    ("analyze_calls", "mode"): "summary (default): compact, deep callers grouped by file; tree: the full tree.",
}


_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.")


def _first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    start = 0
    while (end := text.find(". ", start)) != -1:
        if not any(text[: end + 1].endswith(a) for a in _ABBREVIATIONS):
            return text[: end + 1]
        start = end + 2
    return text


def compact_tool(t: types.Tool, hide_project_path: bool) -> types.Tool:
    """The same tool with a short description and a trimmed schema."""
    hidden = HIDDEN_PARAMS | ({"projectPath"} if hide_project_path else set())
    schema = dict(t.input_schema or {})
    props = {}
    for name, spec in (schema.get("properties") or {}).items():
        if name in hidden:
            continue
        spec = dict(spec)
        if "description" in spec:
            spec["description"] = PARAM_DESCRIPTIONS.get((t.name, name)) or _first_sentence(spec["description"])
        props[name] = spec
    schema["properties"] = props
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r not in hidden]
    # model_copy keeps every other protocol field (annotations, _meta, icons, ...).
    return t.model_copy(
        update={
            "description": DESCRIPTIONS.get(t.name) or _first_sentence(t.description or ""),
            "input_schema": schema,
        }
    )
