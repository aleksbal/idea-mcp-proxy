"""Score benchmark runs against ground truth and print a markdown report.

    python bench/report.py bench/results/<timestamp>
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import tomllib
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.DOTALL)


def extract_answer(text: str) -> dict | None:
    blocks = _JSON_BLOCK.findall(text or "")
    if not blocks:
        return None
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None


def normalize(name: str) -> str:
    """`org.x.Strings#endsWithAny(CharSequence, ...)` -> `strings.endswithany`."""
    name = re.sub(r"\(.*", "", str(name)).replace("#", ".").strip()
    parts = [p for p in name.split(".") if p]
    return ".".join(parts[-2:]).lower() if parts else ""


def score_set(got: list, expected: list) -> dict:
    got_n = {normalize(g) for g in got or []}
    exp_n = {normalize(e) for e in expected}
    tp = len(got_n & exp_n)
    precision = tp / len(got_n) if got_n else (1.0 if not exp_n else 0.0)
    recall = tp / len(exp_n) if exp_n else (1.0 if not got_n else 0.0)
    return {
        "exact": got_n == exp_n,
        "precision": precision,
        "recall": recall,
        "missing": sorted(exp_n - got_n),
        "extra": sorted(got_n - exp_n),
    }


def score(task: dict, answer: dict | None) -> dict:
    if answer is None:
        return {"exact": False, "precision": 0.0, "recall": 0.0, "missing": ["<no parseable answer>"], "extra": []}
    if task["scoring"] == "set":
        return score_set(answer.get(task["answer_key"], []), task["expected"])
    if task["scoring"] == "multi_set":
        parts = {k: score_set(answer.get(k, []), v) for k, v in task["expected_sets"].items()}
        return {
            "exact": all(p["exact"] for p in parts.values()),
            "precision": statistics.mean(p["precision"] for p in parts.values()),
            "recall": statistics.mean(p["recall"] for p in parts.values()),
            "missing": [f"{k}:{m}" for k, p in parts.items() for m in p["missing"]],
            "extra": [f"{k}:{m}" for k, p in parts.items() for m in p["extra"]],
        }
    raise ValueError(f"unknown scoring {task['scoring']!r}")


def total_input(s: dict) -> int:
    return sum(s.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", type=Path)
    parser.add_argument("--tasks", type=Path, default=REPO / "bench" / "tasks.toml")
    args = parser.parse_args()

    tasks = {t["id"]: t for t in tomllib.loads(args.tasks.read_text())["tasks"]}
    summaries = json.loads((args.results / "summaries.json").read_text())
    meta = json.loads((args.results / "meta.json").read_text())

    scored = []
    for s in summaries:
        sc = score(tasks[s["task"]], extract_answer(s["result_text"]))
        scored.append({**s, **sc})
    (args.results / "scored.json").write_text(json.dumps(scored, indent=2))

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in scored:
        groups[(s["task"], s["arm"])].append(s)

    print(f"# Benchmark results\n\nModel `{meta['model']}`, {meta['reps']} rep(s), project {meta['project']['name']} "
          f"`{meta['project']['tag']}`. Values are means over reps.\n")
    print("| task | arm | correct | precision | recall | cost $ | input tok | output tok | turns | IDE calls | built-in calls | wall s |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    mean = lambda rows, f: statistics.mean(f(r) for r in rows)  # noqa: E731
    for (task, arm), rows in sorted(groups.items()):
        print(
            f"| {task} | {arm} | {sum(r['exact'] for r in rows)}/{len(rows)} "
            f"| {mean(rows, lambda r: r['precision']):.2f} | {mean(rows, lambda r: r['recall']):.2f} "
            f"| {mean(rows, lambda r: r['cost_usd'] or 0):.3f} | {mean(rows, total_input):,.0f} "
            f"| {mean(rows, lambda r: r['output_tokens'] or 0):,.0f} | {mean(rows, lambda r: r['num_turns'] or 0):.1f} "
            f"| {mean(rows, lambda r: r['ide_tool_calls']):.1f} | {mean(rows, lambda r: r['builtin_tool_calls']):.1f} "
            f"| {mean(rows, lambda r: r['wall_s']):.0f} |"
        )

    wrong = [s for s in scored if not s["exact"]]
    if wrong:
        print("\n## Incorrect answers\n")
        for s in wrong:
            print(f"- `{s['run_id']}`: missing {s['missing'] or '-'}, extra {s['extra'] or '-'}")


if __name__ == "__main__":
    main()
