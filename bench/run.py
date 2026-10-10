"""Run benchmark tasks headlessly in Claude Code, once per arm.

Each run is one `claude -p` session in the target project with a fixed tool
set. The raw stream-json event log is saved, plus a metrics summary: tokens,
cost, turns, wall time and which tools were called.

    python bench/run.py --tasks bench/tasks.toml --tasks-filter pilot --reps 1
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
PROXY_EXE = REPO / ".venv" / "bin" / "idea-mcp-proxy"
RAW_IDE_URL = "http://127.0.0.1:64342/stream"
# The Claude Code CLI to run; must be logged in (`claude` -> /login).
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"

BUILTIN_TOOLS = "Read,Grep,Glob"

# IDE tools that only read. Everything else the IDE exposes (terminal, run
# configurations, debugger, databases, notebooks, file edits) is denied so
# no arm can do more than look at the code.
IDE_READ_TOOLS = [
    "analyze_calls",
    "search_symbol",
    "search_text",
    "search_regex",
    "search_file",
    "get_symbol_info",
    "read_file",
    "list_directory_tree",
    "get_file_problems",
    "get_project_modules",
    "get_project_dependencies",
]

ARMS = {
    # Claude Code's built-in tools only: the "grep everything" baseline.
    "grep": {"mcpServers": {}},
    # Built-ins plus the IDE tools through this proxy.
    "ide-proxy": {"mcpServers": {"idea": {"type": "stdio", "command": str(PROXY_EXE)}}},
    # Built-ins plus the IDE tools straight from IntelliJ, no proxy. Same
    # server name, so tool names are identical and only the proxy differs.
    "ide-raw": {"mcpServers": {"idea": {"type": "http", "url": RAW_IDE_URL}}},
}

ANSWER_INSTRUCTIONS = (
    "\n\nWork only by reading and searching; do not modify any files. "
    "When you are done, end your reply with a fenced ```json block containing "
    "exactly this structure and nothing else:\n{schema}"
)


def build_command(task: dict, arm: str, mcp_config_path: Path, budget_usd: float, model: str) -> list[str]:
    cmd = [
        CLAUDE_BIN,
        "-p",
        task["prompt"].strip() + ANSWER_INSTRUCTIONS.format(schema=task["answer_schema"].strip()),
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--max-budget-usd", str(budget_usd),
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config", str(mcp_config_path),
        "--tools", BUILTIN_TOOLS,
        # No user/project settings, hooks or permissions leak into a run.
        "--setting-sources", "",
        "--disable-slash-commands",
        "--permission-mode", "dontAsk",
    ]
    allowed = BUILTIN_TOOLS.split(",")
    if arm != "grep":
        allowed += [f"mcp__idea__{t}" for t in IDE_READ_TOOLS]
    cmd += ["--allowedTools", ",".join(allowed)]
    return cmd


def summarize(events: list[dict], wall_s: float, exit_code: int) -> dict:
    tool_calls: Counter[str] = Counter()
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                tool_calls[block["name"]] += 1

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
        "builtin_tool_calls": sum(n for name, n in tool_calls.items() if not name.startswith("mcp__")),
    }


def run_one(task: dict, arm: str, rep: int, out_dir: Path, args) -> dict:
    run_id = f"{task['id']}__{arm}__{rep}"
    mcp_config = out_dir / f"{run_id}.mcp.json"
    mcp_config.write_text(json.dumps(ARMS[arm]))
    cmd = build_command(task, arm, mcp_config, args.budget_usd, args.model)

    print(f"[{datetime.now():%H:%M:%S}] {run_id} ...", flush=True)
    start = time.monotonic()
    proc = subprocess.run(
        cmd, cwd=args.project, capture_output=True, text=True, timeout=args.timeout_s
    )
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
        sys.exit(f"{run_id}: claude produced no result; stderr:\n{proc.stderr[-2000:]}")
    summary = {"run_id": run_id, "task": task["id"], "arm": arm, "rep": rep, **summarize(events, wall, proc.returncode)}
    print(
        f"    cost=${summary['cost_usd']} turns={summary['num_turns']} "
        f"ide_calls={summary['ide_tool_calls']} builtin_calls={summary['builtin_tool_calls']} "
        f"subtype={summary['subtype']}",
        flush=True,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", type=Path, default=REPO / "bench" / "tasks.toml")
    parser.add_argument("--tasks-filter", help="only tasks with this tag")
    parser.add_argument("--task", action="append", help="only these task ids (repeatable)")
    parser.add_argument("--arms", default=",".join(ARMS), help=f"comma-separated subset of {','.join(ARMS)}")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--budget-usd", type=float, default=1.0, help="hard cap per run")
    parser.add_argument("--timeout-s", type=int, default=900)
    parser.add_argument("--out", type=Path, help="results dir (default: bench/results/<timestamp>)")
    args = parser.parse_args()

    spec = tomllib.loads(args.tasks.read_text())
    args.project = Path(spec["project"]["path"]).expanduser()
    tasks = spec["tasks"]
    if args.tasks_filter:
        tasks = [t for t in tasks if args.tasks_filter in t.get("tags", [])]
    if args.task:
        tasks = [t for t in tasks if t["id"] in args.task]
    arms = args.arms.split(",")
    if not tasks or any(a not in ARMS for a in arms):
        sys.exit("no tasks selected, or unknown arm")

    # Absolute: runs execute with the target project as cwd.
    out_dir = (args.out or REPO / "bench" / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"model": args.model, "project": spec["project"], "arms": arms, "reps": args.reps,
            "budget_usd": args.budget_usd, "started": datetime.now(timezone.utc).isoformat()}
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
