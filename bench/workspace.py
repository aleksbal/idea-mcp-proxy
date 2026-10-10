"""The target project checkout: reset between runs, IDE sync, verification.

Edit tasks change the real checkout that IntelliJ has open. Between runs the
checkout is reset with git, and the harness then waits until IntelliJ has
picked the reset up (it lags a few seconds behind the disk) -- otherwise the
next run would work against the previous run's code in the IDE.
"""

from __future__ import annotations

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


def wait_ide_synced(proxy_cmd: list[str], project: Path, files: list[str], probe_symbol: str | None,
                    timeout_s: float = 120) -> float:
    """Block until IntelliJ sees the reset checkout; returns seconds waited.

    Synced means: `probe_symbol` (a name only the edited code had) is no longer
    found, and none of the previously changed Java files shows compile errors
    (the reset checkout compiles). Raises TimeoutError otherwise.
    """
    java = [f for f in files if f.endswith(".java") and (project / f).exists()]

    async def poll() -> float:
        params = StdioServerParameters(command=proxy_cmd[0], args=proxy_cmd[1:], cwd=str(project))
        start = time.monotonic()
        async with stdio_client(params, errlog=open("/dev/null", "w")) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                while time.monotonic() - start < timeout_s:
                    stale = False
                    if probe_symbol:
                        res = await s.call_tool("search_symbol", {"q": probe_symbol})
                        stale = '"filePath"' in res.content[0].text
                    for f in java if not stale else []:
                        res = await s.call_tool("get_file_problems", {"filePath": f, "errorsOnly": True})
                        if '"errors":[]' not in res.content[0].text.replace(" ", ""):
                            stale = True
                            break
                    if not stale:
                        return time.monotonic() - start
                    await anyio.sleep(2)
        raise TimeoutError(f"IntelliJ did not pick up the reset of {files} within {timeout_s}s")

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
