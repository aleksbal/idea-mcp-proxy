"""Run benchmark tasks headlessly in Claude Code: grep only vs grep + IDE.

Each run is one `claude -p` session in the target project. Both arms have the
same built-in tools (Read, Grep, Glob, Edit, Write, and Bash limited to mvn);
the ide-proxy arm additionally has the IntelliJ tools through this proxy, with
its default tool set, descriptions and routing instructions -- i.e. exactly
what a user of the proxy gets.

Before any session, bench/preflight.py must pass (IDE reachable, project
identified, IDE answers every task correctly, checkout clean and compiling).
Edit tasks are verified by compiling and running tests; the checkout is reset
and IntelliJ re-synced after every run.

    python bench/run.py --reps 3
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "bench"))
from idea_mcp_proxy.tool_filter import SEMANTIC_TOOLS  # noqa: E402
from preflight import PreflightError, preflight  # noqa: E402
from workspace import changed_files, ide_error_baseline, reset, verify, wait_ide_synced  # noqa: E402

PROXY_EXE = REPO / ".venv" / "bin" / "idea-mcp-proxy"
# The Claude Code CLI to run; must be logged in (`claude` -> /login).
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"

BUILTIN_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
# Bash only for Maven, so both arms can compile and run tests themselves.
BUILTIN_ALLOWED = ["Read", "Grep", "Glob", "Edit", "Write", "Bash(mvn:*)"]

ARMS = {
    # Claude Code's built-in tools only.
    "grep": {"mcp": {"mcpServers": {}}},
    # Built-ins plus IntelliJ through this proxy, as a user gets it by default.
    "ide-proxy": {"mcp": {"mcpServers": {"idea": {"type": "stdio", "command": str(PROXY_EXE)}}}},
}
DEFAULT_ARMS = ("grep", "ide-proxy")

QUESTION_SUFFIX = (
    "\n\nWork only by reading and searching; do not modify any files. "
    "When you are done, end your reply with a fenced ```json block containing "
    "exactly this structure and nothing else:\n{schema}"
)
EDIT_SUFFIX = "\n\nWhen you are done, reply with DONE."


def build_command(task: dict, arm: str, mcp_config_path: Path, budget_usd: float, model: str) -> list[str]:
    if task.get("kind") == "edit":
        prompt = task["prompt"].strip() + EDIT_SUFFIX
    else:
        prompt = task["prompt"].strip() + QUESTION_SUFFIX.format(schema=task["answer_schema"].strip())
    allowed = list(BUILTIN_ALLOWED)
    if arm != "grep":
        allowed += [f"mcp__idea__{t}" for t in SEMANTIC_TOOLS]
    return [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--model", model,
        "--max-budget-usd", str(budget_usd),
        "--no-session-persistence",
        "--strict-mcp-config", "--mcp-config", str(mcp_config_path),
        "--tools", BUILTIN_TOOLS,
        # No user/project settings, hooks or permissions leak into a run.
        "--setting-sources", "",
        "--disable-slash-commands",
        "--permission-mode", "dontAsk",
        "--allowedTools", ",".join(allowed),
    ]


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def summarize(events: list[dict], wall_s: float, exit_code: int) -> dict:
    tool_calls: Counter[str] = Counter()
    ide_ids: set[str] = set()
    ide_ok = ide_failed = 0
    ide_errors: list[str] = []
    for ev in events:
        message = ev.get("message")
        for block in message.get("content", []) if isinstance(message, dict) else []:
            if not isinstance(block, dict):
                continue
            if ev.get("type") == "assistant" and block.get("type") == "tool_use":
                tool_calls[block["name"]] += 1
                if block["name"].startswith("mcp__idea__"):
                    ide_ids.add(block["id"])
            elif ev.get("type") == "user" and block.get("type") == "tool_result" and block.get("tool_use_id") in ide_ids:
                # The IDE reports some failures (e.g. "Unable to determine the target
                # project") as plain text rather than is_error, so check both.
                text = _result_text(block)
                if block.get("is_error") or "Unable to determine the target project" in text:
                    ide_failed += 1
                    ide_errors.append(text[:300])
                else:
                    ide_ok += 1

    result = next((ev for ev in reversed(events) if ev.get("type") == "result"), {})
    usage = result.get("usage", {})
    return {
        "exit_code": exit_code,
        "subtype": result.get("subtype"),
        "is_error": result.get("is_error"),
        "result_text": result.get("result", ""),
        "cost_usd": result.get("total_cost_usd"),
        "num_turns": result.get("num_turns"),
        "duration_ms": result.get("duration_ms"),
        "wall_s": round(wall_s, 1),
        "input_tokens": usage.get("input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "tool_calls": dict(tool_calls),
        "ide_tool_calls": sum(n for name, n in tool_calls.items() if name.startswith("mcp__idea__")),
        "ide_calls_ok": ide_ok,
        "ide_calls_failed": ide_failed,
        "ide_errors": ide_errors,
        "builtin_tool_calls": sum(n for name, n in tool_calls.items() if not name.startswith("mcp__")),
        "bash_calls": tool_calls.get("Bash", 0),
    }


def run_one(task: dict, arm: str, rep: int, out_dir: Path, args) -> dict:
    run_id = f"{task['id']}__{arm}__{rep}"
    if changed_files(args.project):
        sys.exit(f"{run_id}: checkout is not clean before the run -- refusing to start")
    mcp_config = out_dir / f"{run_id}.mcp.json"
    mcp_config.write_text(json.dumps(ARMS[arm]["mcp"]))
    budget = args.edit_budget_usd if task.get("kind") == "edit" else args.budget_usd
    cmd = build_command(task, arm, mcp_config, budget, args.model)

    print(f"[{datetime.now():%H:%M:%S}] {run_id} ...", flush=True)
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=args.project, capture_output=True, text=True, timeout=args.timeout_s)
    wall = time.monotonic() - start

    (out_dir / f"{run_id}.jsonl").write_text(proc.stdout)
    if proc.stderr:
        (out_dir / f"{run_id}.stderr.txt").write_text(proc.stderr)
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if not any(ev.get("type") == "result" for ev in events):
        # Never got as far as a session (bad flag, missing config, auth): a
        # setup error, not a benchmark result -- stop instead of recording it.
        reset(args.project)
        sys.exit(f"{run_id}: claude produced no result; stderr:\n{proc.stderr[-2000:]}")

    summary = {"run_id": run_id, "task": task["id"], "arm": arm, "rep": rep,
               "case": task.get("case"), "kind": task.get("kind", "question"),
               **summarize(events, wall, proc.returncode)}

    touched = changed_files(args.project)
    summary["files_changed"] = touched
    if task.get("kind") == "edit":
        summary["verification"] = verify(args.project, task["verify"])
        (out_dir / f"{run_id}.diff").write_text(
            subprocess.run(["git", "-C", str(args.project), "diff"], capture_output=True, text=True).stdout)
    if touched:
        reset(args.project)
        summary["ide_sync_s"] = round(
            wait_ide_synced([str(PROXY_EXE)], args.project, touched, task.get("sync_probe"), args.ide_baseline), 1)

    # Valid = the IDE never failed when called. Whether it was called at all is
    # reported separately (ide_used): on grep cases not calling it is right.
    summary["valid"] = arm == "grep" or summary["ide_calls_failed"] == 0
    summary["ide_used"] = summary["ide_calls_ok"] > 0
    verdict = summary.get("verification", {}).get("passed")
    print(
        f"    cost=${summary['cost_usd']:.3f} turns={summary['num_turns']} "
        f"ide ok/failed={summary['ide_calls_ok']}/{summary['ide_calls_failed']} "
        f"builtin={summary['builtin_tool_calls']} valid={summary['valid']}"
        + (f" verified={verdict}" if verdict is not None else ""),
        flush=True,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", type=Path, default=REPO / "bench" / "tasks.toml")
    parser.add_argument("--task", action="append", help="only these task ids (repeatable)")
    parser.add_argument("--arms", default=",".join(DEFAULT_ARMS), help=f"comma-separated subset of {','.join(ARMS)}")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--budget-usd", type=float, default=1.0, help="hard cap per question run")
    parser.add_argument("--edit-budget-usd", type=float, default=2.0, help="hard cap per edit run")
    parser.add_argument("--timeout-s", type=int, default=1200)
    parser.add_argument("--out", type=Path, help="results dir (default: bench/results/<timestamp>)")
    args = parser.parse_args()

    spec = tomllib.loads(args.tasks.read_text())
    args.project = Path(spec["project"]["path"]).expanduser()
    tasks = spec["tasks"]
    if args.task:
        tasks = [t for t in tasks if t["id"] in args.task]
    arms = args.arms.split(",")
    if not tasks or any(a not in ARMS for a in arms):
        sys.exit("no tasks selected, or unknown arm")

    try:
        for line in preflight({a: ARMS[a] for a in arms}, args.project, tasks, spec["project"].get("ide_health_files", [])):
            print("preflight ok:", line, flush=True)
    except PreflightError as exc:
        sys.exit(f"PREFLIGHT FAILED -- nothing was run: {exc}")

    print("recording IntelliJ's errors on the untouched checkout (once per commit, ~4 min)...", flush=True)
    args.ide_baseline = ide_error_baseline(
        [str(PROXY_EXE)], args.project, REPO / "bench" / "results" / "ide_baseline.json")
    print(f"baseline: {len(args.ide_baseline)} files, "
          f"{sum(1 for e in args.ide_baseline.values() if e)} with IDE-only errors", flush=True)

    # Absolute: runs execute with the target project as cwd.
    out_dir = (args.out or REPO / "bench" / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"model": args.model, "project": spec["project"], "arms": arms, "reps": args.reps,
            "budget_usd": args.budget_usd, "edit_budget_usd": args.edit_budget_usd,
            "tasks": [t["id"] for t in tasks], "started": datetime.now(timezone.utc).isoformat()}
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    summaries = []
    # Interleave arms per task and rep so drift (IDE state, API latency)
    # affects all arms alike.
    for rep in range(1, args.reps + 1):
        for task in tasks:
            for arm in arms:
                summaries.append(run_one(task, arm, rep, out_dir, args))
                (out_dir / "summaries.json").write_text(json.dumps(summaries, indent=2))

    total = sum(s["cost_usd"] or 0 for s in summaries)
    print(f"done: {len(summaries)} runs, total ${total:.2f}, results in {out_dir}")


if __name__ == "__main__":
    main()
