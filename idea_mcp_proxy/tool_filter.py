"""Which upstream IDE tools the proxy exposes.

The JetBrains server exposes ~60 tools (debugger, databases, notebooks, run
configurations, a terminal ...). Every tool definition is sent to the model
on every request, so the unused ones cost context tokens on every turn and
bury the code-intelligence tools the agent should be reaching for. The
proxy exposes a curated set by default and refuses calls to anything hidden.

Patterns are fnmatch globs (`search_*`) or `@preset` names; an empty
include list means "everything".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase

# Reading and navigating code: nothing here changes files or runs anything.
CODE_READ_TOOLS = (
    "analyze_calls",
    "search_symbol",
    "search_text",
    "search_regex",
    "search_file",
    "get_symbol_info",
    "read_file",
    "list_directory_tree",
    "get_file_problems",
    "lint_files",
    "get_project_modules",
    "get_project_dependencies",
    "get_all_open_file_paths",
    # Readiness check (indexing/scanning) JetBrains recommends before the
    # heavier analysis tools; not exposed by every IDE version.
    "get_project_status",
)

# Plus the IDE's code-aware editing, building and VCS status. Deliberately not
# included: execute_terminal_command, run configurations, the debugger,
# databases, notebooks -- and execute_tool, which invokes any tool by name
# and would make the filter pointless.
CODE_TOOLS = CODE_READ_TOOLS + (
    "rename_refactoring",
    "apply_patch",
    "create_new_file",
    "reformat_file",
    "build_project",
    "open_file_in_editor",
    "get_repositories",
    "git_status",
)

# Only what the agent's own Grep/Glob/Read/Edit can't do: call hierarchy,
# symbol lookup, types and docs at a position, compile errors without a build,
# refactoring. IDE text search, file reading, file listing and patching
# duplicate the built-ins and only compete with them, so they're left out.
SEMANTIC_READ_TOOLS = (
    "analyze_calls",
    "search_symbol",
    "get_symbol_info",
    "get_file_problems",
    "lint_files",
    "get_project_modules",
    "get_project_dependencies",
    "get_project_status",
)
SEMANTIC_TOOLS = SEMANTIC_READ_TOOLS + (
    "build_project",
    "rename_refactoring",
    "reformat_file",
)

PRESETS: dict[str, tuple[str, ...]] = {
    "semantic": SEMANTIC_TOOLS,
    "semantic-read": SEMANTIC_READ_TOOLS,
    "code": CODE_TOOLS,
    "code-read": CODE_READ_TOOLS,
}

DEFAULT_INCLUDE = ("@semantic",)


class UnknownPreset(ValueError):
    pass


def _expand(patterns: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for p in patterns:
        p = p.strip()
        if not p:
            continue
        if p.startswith("@"):
            try:
                out.extend(PRESETS[p[1:]])
            except KeyError:
                raise UnknownPreset(
                    f"unknown tool preset {p!r}; known: {', '.join('@' + k for k in PRESETS)}"
                ) from None
        else:
            out.append(p)
    return tuple(out)


@dataclass(frozen=True)
class ToolFilter:
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_patterns(cls, include: Iterable[str], exclude: Iterable[str] = ()) -> ToolFilter:
        return cls(include=_expand(include), exclude=_expand(exclude))

    def allows(self, name: str) -> bool:
        return not self.is_excluded(name) and (
            not self.include or any(fnmatchcase(name, p) for p in self.include)
        )

    def is_excluded(self, name: str) -> bool:
        return any(fnmatchcase(name, p) for p in self.exclude)

    def refusal(self, name: str) -> str:
        """Why `name` is hidden, phrased so the fix is actionable."""
        if self.is_excluded(name):
            # Exclusions win over inclusions, so --include-tools can't help.
            return (
                f"Tool {name!r} is not exposed by idea-mcp-proxy: it matches "
                "--exclude-tools / $IDEA_MCP_EXCLUDE_TOOLS. Remove it from there to enable it."
            )
        return (
            f"Tool {name!r} is not exposed by idea-mcp-proxy. "
            "Add it to --include-tools / $IDEA_MCP_INCLUDE_TOOLS to enable it."
        )


def parse_patterns(value: str | None) -> list[str]:
    """`"@code, search_*"` -> `["@code", "search_*"]`; None/"" -> []."""
    return [p.strip() for p in (value or "").split(",") if p.strip()]
