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

# When-to-use rules, agreed per task type. Each IDE tool's description starts
# with the tasks it should be used for *instead of* the built-in tools; the
# server instructions (ROUTING_INSTRUCTIONS) repeat the whole table once.
DESCRIPTIONS: dict[str, str] = {
    "rename_refactoring": (
        "Use for EVERY rename of a class, method, field, parameter or variable, instead of "
        "Edit/sed. The IDE updates all references project-wide and gets overloads, "
        "overrides, imports and same-named symbols right; text replacement doesn't."
    ),
    "build_project": (
        "Use ALWAYS after changing a signature, renaming, or moving code, to check that the "
        "whole project still compiles. Returns the compilation errors. Pass `filesToRebuild` "
        "to compile only some files."
    ),
    "get_file_problems": (
        "Use right after editing a file: its compile errors and warnings from the IDE, "
        "without running a build."
    ),
    "lint_files": "Like get_file_problems, for several edited files in one call.",
    "analyze_calls": (
        "Use ALWAYS for who-calls-what questions (callers, impact of a change, is it unused), "
        "instead of grepping for the name. The IDE resolves the exact overload, calls "
        "through interfaces and overrides, and indirect callers many levels deep in one call. "
        "INCOMING_CALLS = callers, OUTGOING_CALLS = callees. symbolFqn: fully qualified, "
        "e.g. `com.x.Foo.bar`, or `com.x.Foo.bar(String, int)` for one overload; an "
        "ambiguous name returns the exact signatures to pick from."
    ),
    "get_symbol_info": (
        "Use when you need the type, signature or declaration of something at "
        "file:line:column and the text doesn't show it: `var`, lambdas, generics, inherited "
        "or overloaded members. Returns IntelliJ's Quick Documentation."
    ),
    "search_symbol": (
        "Use to find where a class, method or field is declared, instead of Grep/Glob: "
        "returns declarations only, no text matches. For text, log messages, string "
        "literals or config keys, use Grep."
    ),
    "get_project_modules": (
        "Use for the project's module structure, instead of reading build files."
    ),
    "get_project_dependencies": (
        "Use for the libraries the project depends on, instead of reading pom.xml/"
        "build.gradle: resolved by the IDE, including parent POMs and BOMs."
    ),
    "reformat_file": "Apply the project's code style to the given files.",
    "get_project_status": "Whether the IDE is still indexing; check if IDE calls time out.",
}

ROUTING_INSTRUCTIONS = """\
IntelliJ IDEA for this project, through idea-mcp-proxy. Use the IDE where text \
search can't be exact, and the built-in tools for everything else:
- Rename a code symbol: rename_refactoring (never Edit/sed).
- After a signature change, rename or move: build_project, always.
- After editing a file: get_file_problems (or lint_files for several).
- Who calls / impact / unused: analyze_calls, always (not Grep).
- Type or signature not visible in the text: get_symbol_info.
- Where is a class/method declared: search_symbol.
- Modules / dependencies: get_project_modules / get_project_dependencies.
- Text, log messages, string literals, config keys, comments: Grep. Files by name: \
Glob. Reading files: Read. Other edits: Edit.
"""

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
