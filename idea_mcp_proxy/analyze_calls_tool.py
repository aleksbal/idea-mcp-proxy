"""The one wrapped tool: analyze_calls.

Adds `paths`, `sourceScope`, and `mode` on top of the real tool's schema,
calls straight through to the real server otherwise, and locally trims the
response before it goes back to the caller. Falls back to the raw upstream
response, untouched, if parsing the tree text fails for any reason -- a
format change upstream should degrade to "no worse than before", not error.
"""

from __future__ import annotations

import logging

import mcp.types as types

from .summarize import filter_by_paths, filter_by_scope, render_summary, render_tree
from .tool_descriptions import with_disambiguation_note
from .tree_parser import TreeParseError, parse_tree
from .upstream import UpstreamConnection

logger = logging.getLogger(__name__)

WRAPPED_TOOL_NAME = "analyze_calls"

# Extra properties layered on top of the real tool's inputSchema.
_EXTRA_PROPERTIES: dict[str, object] = {
    "paths": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Glob patterns (relative to project root, '!' for excludes) to filter "
            "which caller files are kept, e.g. ['!**/test/**']. Applied locally by "
            "the proxy after fetching results from the IDE."
        ),
    },
    "sourceScope": {
        "type": "string",
        "enum": ["production", "test", "all"],
        "description": (
            "Keep only callers whose file is production or test source (heuristic: "
            "a /test/ path segment or *Test/*Tests/*IT/*Spec filename). Default: all."
        ),
    },
    "mode": {
        "type": "string",
        "enum": ["summary", "tree"],
        "description": (
            "'summary' (default): direct callers listed individually, deeper callers "
            "grouped by file with test callers collapsed to a count above a threshold. "
            "'tree': full re-serialized tree, still deduplicated (ancestry via "
            "indentation, not a repeated treePath string per node)."
        ),
    },
}


def build_wrapped_tool(real_tool: types.Tool) -> types.Tool:
    schema = dict(real_tool.input_schema)
    schema["properties"] = {**schema.get("properties", {}), **_EXTRA_PROPERTIES}
    description = (real_tool.description or "") + (
        "\n\n[proxied] Supports extra `paths`/`sourceScope`/`mode` params to trim "
        "output before it reaches the caller; see their descriptions."
    )
    description = with_disambiguation_note(description, real_tool.name)
    return types.Tool(
        name=real_tool.name,
        title=real_tool.title,
        description=description,
        input_schema=schema,
        output_schema=real_tool.output_schema,
    )


async def handle_analyze_calls(
    upstream: UpstreamConnection, arguments: dict[str, object]
) -> types.CallToolResult:
    args = dict(arguments)
    paths_filter = args.pop("paths", None)
    source_scope = args.pop("sourceScope", "all")
    mode = args.pop("mode", "summary")

    result = await upstream.call_tool(WRAPPED_TOOL_NAME, args)

    raw_text_parts = [c.text for c in result.content if isinstance(c, types.TextContent)]
    raw_text = "\n".join(raw_text_parts)

    if result.is_error or not raw_text.strip():
        return result

    symbol_fqn = str(args.get("symbolFqn", ""))

    try:
        root = parse_tree(raw_text, root_signature=symbol_fqn)
    except TreeParseError:
        # Format didn't match what we expected -- pass the raw response
        # through unmodified rather than risk a corrupted/partial tree.
        logger.warning(
            "analyze_calls tree parse failed for symbolFqn=%r; falling back to raw passthrough",
            symbol_fqn,
            exc_info=True,
        )
        return result

    if paths_filter:
        include = [p for p in paths_filter if not p.startswith("!")]
        exclude = [p[1:] for p in paths_filter if p.startswith("!")]
        root = filter_by_paths(root, include or None, exclude or None)

    if source_scope != "all":
        root = filter_by_scope(root, source_scope)  # type: ignore[arg-type]

    output_text = render_summary(root) if mode == "summary" else render_tree(root)

    return types.CallToolResult(content=[types.TextContent(type="text", text=output_text)])
