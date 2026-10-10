"""Verify, before any benchmark session, that each IDE arm really works.

For every IDE arm this connects to the `idea` MCP server exactly as that arm's
Claude Code sessions will (same command and args, same working directory) and
checks that:

1. the IDE is reachable and identifies the benchmark project;
2. for every task the IDE can answer, the IDE's own answer to the task's
   question (the task's `ide_check` call) contains every expected item.

If any check fails the benchmark must not run: a comparison against an IDE
that errors, targets the wrong project, or can't produce the answer says
nothing about the IDE.

    python bench/preflight.py            # standalone; run.py calls it too
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent


class PreflightError(Exception):
    pass


def _text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


def _caller_lines(text: str) -> list[str]:
    # Every rendered node except the root (the queried symbol itself).
    lines = [ln for ln in text.splitlines() if ".java" in ln]
    return lines[1:]


def _found(lines: list[str], item: str, kind: str) -> bool:
    """Is `Class.method` (or just `Class` for kind=class) among the IDE's callers?"""
    for alt in item.split("|"):
        parts = alt.split(".")
        cls, method = (parts[-1], None) if kind == "class" else (parts[-2], parts[-1])
        # A nested class's methods are rendered with the file of its outer class,
        # so match the method plus either the class name or a file of that name.
        for ln in lines:
            if method and not re.search(rf"\b{re.escape(method)}\(", ln):
                continue
            if re.search(rf"[/\"]{re.escape(cls)}\.java", ln) or (method and cls in ln):
                return True
    return False


def check_task(task: dict, text: str) -> list[str]:
    """Expected items missing from the IDE's answer (empty list = pass)."""
    lines = _caller_lines(text)
    kind = task.get("normalize", "method")
    if task["scoring"] == "set":
        return [e for e in task["expected"] if not _found(lines, e, kind)]
    problems = []
    for key, expected in task["expected_sets"].items():
        if not expected:
            # An expected-empty set (e.g. "no production callers"): the IDE must
            # not report any caller in production code.
            if key == "production_callers" and any("src/main/java" in ln for ln in lines):
                problems.append(f"{key}: IDE reports production callers, expected none")
            continue
        problems += [f"{key}:{e}" for e in expected if not _found(lines, e, "class")]
    return problems


async def _check_arm(arm: str, server: dict, project: Path, tasks: list[dict], health_files: list[str]) -> list[str]:
    log = []

    async def run(session: ClientSession) -> None:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        needed = {"analyze_calls"} | (
            {"rename_refactoring", "build_project", "get_file_problems"}
            if any(t.get("kind") == "edit" for t in tasks) else set())
        if needed - names:
            raise PreflightError(f"{arm}: IDE tools not exposed: {sorted(needed - names)} (have: {sorted(names)})")

        # No projectPath: the proxy must resolve the project on its own, exactly
        # as it has to for the agent.
        base: dict = {}
        res = await session.call_tool("get_project_modules", base)
        text = _text(res)
        if res.is_error or '"modules"' not in text:
            raise PreflightError(f"{arm}: IDE did not identify the project: {text[:300]}")
        log.append(f"{arm}: project identified -> {text.strip()[:120]}")

        # The IDE must have the project fully configured (e.g. imported as a Maven
        # project, test dependencies resolved): these files compile cleanly, so
        # the IDE must report no errors in them. Otherwise its compile checks
        # would report false errors to the agent.
        for f in health_files:
            res = await session.call_tool("get_file_problems", {"filePath": f, "errorsOnly": True})
            problems = _text(res)
            if res.is_error or '"errors":[]' not in problems.replace(" ", ""):
                raise PreflightError(
                    f"{arm}: IntelliJ reports errors in {f}, which compiles fine with Maven -- "
                    f"the IDE project isn't fully set up (Maven import / dependencies?): {problems[:300]}")
        if health_files:
            log.append(f"{arm}: IDE reports no errors in {len(health_files)} known-good files")

        for task in tasks:
            check = task.get("ide_check")
            if not check:
                why = "edit task, verified by compile/tests" if task.get("kind") == "edit" else "grep case"
                log.append(f"{arm}: {task['id']}: no IDE answer check ({why})")
                continue
            res = await session.call_tool(check["tool"], {**base, **check["args"]})
            text = _text(res)
            if res.is_error:
                raise PreflightError(f"{arm}: {task['id']}: IDE call failed: {text[:300]}")
            missing = check_task(task, text)
            if missing:
                raise PreflightError(f"{arm}: {task['id']}: IDE answer lacks {missing}")
            log.append(f"{arm}: {task['id']}: IDE answer contains all expected items")

    with anyio.fail_after(300):
        if server["type"] == "stdio":
            params = StdioServerParameters(command=server["command"], args=server.get("args", []), cwd=str(project))
            async with stdio_client(params, errlog=open("/dev/null", "w")) as (r, w):
                async with ClientSession(r, w) as session:
                    await run(session)
        else:
            raise PreflightError(f"{arm}: unsupported server type {server['type']!r}")
    return log


def _check_checkout(project: Path, tasks: list[dict]) -> list[str]:
    from workspace import changed_files, maven, verify  # noqa: PLC0415

    if changed_files(project):
        raise PreflightError(f"checkout {project} is not clean: {changed_files(project)[:5]}")
    ok, out = maven(project, "-DskipTests", "test-compile")
    if not ok:
        raise PreflightError(f"the untouched checkout does not compile:\n{out[-1000:]}")
    log = ["checkout clean, main and test sources compile"]
    for task in tasks:
        if task.get("kind") != "edit":
            continue
        # The declaration checks must fail on the untouched code, or a run that
        # does nothing would pass.
        untouched = verify(project, {k: v for k, v in task["verify"].items() if k in ("contains", "lacks")})
        if untouched["passed"]:
            raise PreflightError(f"{task['id']}: verification already passes on the untouched checkout")
        log.append(f"{task['id']}: verification fails on untouched code, as it must")
    return log


def preflight(arms: dict[str, dict], project: Path, tasks: list[dict], health_files: list[str] = ()) -> list[str]:
    """Raises PreflightError on the first failed check; returns a log of passed ones."""
    log = _check_checkout(project, tasks)
    for arm, spec in arms.items():
        server = spec["mcp"]["mcpServers"].get("idea")
        if server is None:
            continue
        try:
            log += anyio.run(_check_arm, arm, server, project, tasks, list(health_files))
        except BaseExceptionGroup as group:
            # Raised inside the MCP client's task group; surface our own error.
            leaf = group
            while isinstance(leaf, BaseExceptionGroup):
                leaf = leaf.exceptions[0]
            raise leaf from None
    return log


def main() -> None:
    sys.path.insert(0, str(REPO / "bench"))
    from run import ARMS, DEFAULT_ARMS  # noqa: PLC0415

    spec = tomllib.loads((REPO / "bench" / "tasks.toml").read_text())
    project = Path(spec["project"]["path"]).expanduser()
    try:
        for line in preflight({a: ARMS[a] for a in DEFAULT_ARMS}, project, spec["tasks"], spec["project"].get("ide_health_files", [])):
            print("ok  ", line)
    except PreflightError as exc:
        sys.exit(f"PREFLIGHT FAILED: {exc}")
    print("preflight passed")


if __name__ == "__main__":
    main()
