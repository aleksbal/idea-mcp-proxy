"""The target project checkout: reset between runs, IDE sync, verification.

Edit tasks change the real checkout that IntelliJ has open. Between runs the
checkout is reset with git, and the harness then waits until IntelliJ has
picked the reset up (it lags a few seconds behind the disk) -- otherwise the
next run would work against the previous run's code in the IDE.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MAVEN = [
    "mvn", "-q", "-B", "-o",
    # Only compile and run tests; skip the release/quality plugins.
    "-Drat.skip=true", "-Dcheckstyle.skip=true", "-Djacoco.skip=true",
    "-Danimal.sniffer.skip=true", "-Dspotbugs.skip=true", "-Dpmd.skip=true",
    "-Dcyclonedx.skip=true", "-Djapicmp.skip=true", "-Dmaven.javadoc.skip=true",
]


def _git(project: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, check=True).stdout


def changed_files(project: Path) -> list[str]:
    """Modified and untracked files, relative to the project."""
    out = _git(project, "status", "--porcelain", "--untracked-files=all")
    return [line[3:] for line in out.splitlines() if line.strip()]


def reset(project: Path) -> list[str]:
    """Discard every change to the checkout; returns what was changed."""
    files = changed_files(project)
    if files:
        _git(project, "checkout", "-q", "--", ".")
        _git(project, "clean", "-fdq", "--", "src")
    return files


def _errors(text: str) -> list[list]:
    """IntelliJ's errors for one file, as comparable [line, description] pairs."""
    try:
        return sorted([e.get("line"), e.get("description")] for e in json.loads(text).get("errors", []))
    except (json.JSONDecodeError, AttributeError):
        return [[None, text[:200]]]


def ide_error_baseline(proxy_cmd: list[str], project: Path, cache: Path) -> dict[str, list]:
    """IntelliJ's errors for every Java file of the untouched checkout.

    Some files show IDE errors that don't affect compilation (e.g. javadoc
    links to libraries that aren't dependencies), so "synced" can't mean "no
    errors" -- it means "the same errors as the untouched checkout". Computed
    once (~0.4 s per file) and cached per checkout commit.
    """
    head = _git(project, "rev-parse", "HEAD").strip()
    if cache.exists():
        data = json.loads(cache.read_text())
        if data.get("head") == head:
            return data["errors"]
    files = sorted(str(f.relative_to(project)) for f in (project / "src").rglob("*.java"))

    async def collect() -> dict[str, list]:
        params = StdioServerParameters(command=proxy_cmd[0], args=proxy_cmd[1:], cwd=str(project))
        out = {}
        async with stdio_client(params, errlog=open("/dev/null", "w")) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for f in files:
                    res = await s.call_tool("get_file_problems", {"filePath": f, "errorsOnly": True})
                    out[f] = _errors(res.content[0].text)
        return out

    errors = anyio.run(collect)
    cache.write_text(json.dumps({"head": head, "errors": errors}))
    return errors


def wait_ide_synced(proxy_cmd: list[str], project: Path, files: list[str], probe_symbol: str | None,
                    baseline: dict[str, list], timeout_s: float = 180) -> float:
    """Block until IntelliJ sees the reset checkout; returns seconds waited.

    Synced means: `probe_symbol` (a name only the edited code had) is no longer
    found, and every previously changed Java file shows exactly the errors it
    shows in the untouched checkout (`baseline`). Raises TimeoutError otherwise.
    """
    java = [f for f in files if f.endswith(".java") and (project / f).exists()]

    async def poll() -> float:
        params = StdioServerParameters(command=proxy_cmd[0], args=proxy_cmd[1:], cwd=str(project))
        start = time.monotonic()
        last = ""
        async with stdio_client(params, errlog=open("/dev/null", "w")) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                while time.monotonic() - start < timeout_s:
                    stale = None
                    if probe_symbol:
                        res = await s.call_tool("search_symbol", {"q": probe_symbol})
                        if '"filePath"' in res.content[0].text:
                            stale = f"IDE still finds {probe_symbol!r}"
                    for f in java if stale is None else []:
                        res = await s.call_tool("get_file_problems", {"filePath": f, "errorsOnly": True})
                        if _errors(res.content[0].text) != baseline.get(f, []):
                            stale = f"{f}: errors differ from the untouched checkout"
                            break
                    if stale is None:
                        return time.monotonic() - start
                    last = stale
                    await anyio.sleep(2)
        raise TimeoutError(f"IntelliJ did not pick up the reset within {timeout_s}s: {last}")

    return anyio.run(poll)


def maven(project: Path, *goals: str, timeout_s: int = 900) -> tuple[bool, str]:
    proc = subprocess.run([*MAVEN, *goals], cwd=project, capture_output=True, text=True, timeout=timeout_s)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]


def verify(project: Path, spec: dict) -> dict:
    """Check an edit task's result on disk: compile, tests, declarations."""
    result: dict = {"checks": []}

    def check(name: str, ok: bool, detail: str = "") -> None:
        result["checks"].append({"check": name, "ok": ok, **({"detail": detail} if detail else {})})

    for path, pattern in spec.get("contains", []):
        text = (project / path).read_text() if (project / path).exists() else ""
        check(f"contains {path}: {pattern}", re.search(pattern, text) is not None)
    for path, pattern in spec.get("lacks", []):
        text = (project / path).read_text() if (project / path).exists() else ""
        check(f"lacks {path}: {pattern}", re.search(pattern, text) is None)
    if spec.get("compile"):
        ok, log = maven(project, "-DskipTests", "test-compile")
        check("main and test sources compile", ok, "" if ok else log)
        if ok and spec.get("tests"):
            ok, log = maven(project, "test", "-Dtest=" + ",".join(spec["tests"]), "-Dsurefire.failIfNoSpecifiedTests=false")
            check(f"tests pass: {', '.join(spec['tests'])}", ok, "" if ok else log)
    result["passed"] = all(c["ok"] for c in result["checks"])
    return result
