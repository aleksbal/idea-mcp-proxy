# Run 3 data

The data behind `bench/RESULTS.md`: 48 runs (8 tasks × grep / ide-proxy × 3
repetitions) on Apache Commons Lang 3.21.0 with IntelliJ IDEA 2026.2, model
`claude-sonnet-5-5`, Claude Code 2.1, total cost $2.88.

| file | content |
|---|---|
| `report.md` | the scored report (`python bench/report.py <dir>`) |
| `summaries.json` | per run: cost, tokens, turns, wall time, tool calls (IDE ok/failed), validity, edit verification checks |
| `scored.json` | the same plus the score of each answer (missing/extra items) |
| `meta.json` | model, project, arms, repetitions, budgets |
| `diffs/` | what each edit run changed in the checkout (verified, then reset) |
| `transcripts.tar.gz` | the full stream-json transcript of every session: every prompt, tool call, tool result and answer |

To re-score: extract the transcripts next to `summaries.json`/`meta.json` and run
`python bench/report.py bench/runs/run-3`.
