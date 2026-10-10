# Benchmark results: grep only vs grep + IntelliJ (through the proxy)

Run 3, 2026-10-10. Model: `claude-sonnet-5-5` (Claude Code 2.1, headless `claude -p`).
Project: Apache Commons Lang `rel/commons-lang-3.21.0` (~105k lines main, 363
test files, one Maven module), open in IntelliJ IDEA 2026.2. 8 tasks x 2 arms x
3 repetitions = 48 runs, total cost $2.88. All data -- report, per-run metrics,
edit diffs and the full transcript of every session -- is in
[`runs/run-3/`](runs/run-3/).

## Question and decision rule

Not "can the IDE replace grep", but: **does an agent with grep + IntelliJ do
better than an agent with grep only?** Agreed before running: the IDE is worth
it only if, on the tasks the routing rules send to the IDE, grep + IDE is
measurably better on at least one of precision, cost or speed, and it is not
worse on the tasks where grep is the right tool.

## Setup

- **grep**: Claude Code's Read, Grep, Glob, Edit, Write, and Bash limited to
  `mvn` (so it can compile and run tests itself).
- **ide-proxy**: the same, plus IntelliJ through this proxy as a user gets it by
  default: the 10 `@semantic` tools, compact when-to-use descriptions, routing
  rules as MCP server instructions, `projectPath` filled in by the proxy.
- No user settings, hooks or other MCP servers in either arm.

Tasks (`tasks.toml`), by the routing rules in the README:

| task | case | what |
|---|---|---|
| grep-literal-files | grep | files containing the literal `"os.arch"` |
| grep-message-methods | grep | methods throwing a given exception message |
| overload-callers | ide | callers of `ArrayUtils.isEmpty(Object[])` among 9 overloads (5 `char[]` traps) |
| zero-callers | ide | production callers of `ObjectUtils.min` (none; ~89 grep hits for `min(`) |
| transitive-callers | ide | all direct and indirect callers of `MemberUtils.isMatchingMethod` (7 levels) |
| impact-signature | ide | callers of exactly `StringUtils.repeat(char, int)` |
| rename-method | ide, edit | rename `SerializationUtils.clone` to `deepClone` (10 files; `clone` exists in 5 other classes) |
| change-signature | ide, edit | swap the parameters of `StringUtils.repeat(char, int)`; a missed caller still compiles but is wrong |

Validity, enforced by the harness rather than assumed:

- **Preflight** aborts before any session unless the IDE identifies the project,
  reports no errors in known-good files (caught an IDE project without its test
  dependencies), and its own answer to every navigation task contains the whole
  answer key; edit verification must fail on the untouched checkout, and a
  known-good solution was checked to pass it.
- **Edits** are verified independently of the IDE: declaration checks, Maven
  compile of main + test sources, and the affected test classes.
- The checkout is reset after every run, and the next run waits until IntelliJ
  has picked the reset up.
- A run counts only if no IDE call failed. In run 3: all 48 runs valid.

## Results

Totals per case:

| case | arm | correct | cost | mean turns | mean wall time | runs using the IDE |
|---|---|---|---|---|---|---|
| ide (18 runs) | grep | 17/18 | $1.37 | 12.0 | 31 s | 0/18 |
| ide (18 runs) | grep + IDE | **18/18** | $1.32 | **8.4** | 32 s | 18/18 |
| grep (6 runs) | grep | 4/6 | $0.08 | 3.0 | 14 s | 0/6 |
| grep (6 runs) | grep + IDE | 6/6 | $0.10 | 2.8 | 11 s | 0/6 |

Per task (means over 3 runs):

| task | grep: correct, cost, turns, time | grep + IDE: correct, cost, turns, time |
|---|---|---|
| rename-method | 3/3, $0.104, 25.7, 45 s | 3/3, **$0.063, 6.3, 29 s** |
| overload-callers | 3/3, $0.108, 12.7, 31 s | 3/3, **$0.080, 7.3**, 36 s |
| transitive-callers | 2/3, $0.079, 10.7, 36 s | **3/3**, $0.093, 10.3, 52 s |
| impact-signature | 3/3, $0.036, 3.7, 22 s | 3/3, $0.032, 4.0, 17 s |
| zero-callers | 3/3, $0.042, 3.3, 12 s | 3/3, $0.038, 4.3, 15 s |
| change-signature | 3/3, **$0.087**, 16.0, **38 s** | 3/3, $0.134, 18.0, 46 s |
| grep-literal-files | 1/3, $0.010, 2.0, 11 s | 3/3, $0.010, 2.0, 8 s |
| grep-message-methods | 3/3, $0.018, 4.0, 17 s | 3/3, $0.025, 3.7, 14 s |

The two `grep-literal-files` misses (grep arm, `ArchUtils` missing) and the
`transitive-callers` miss (grep arm, a same-named `invoke` in the wrong class)
are not IDE-related; with 3 repetitions they are within run-to-run noise.

## Verdict

- **Grep cases: not worse.** The IDE was available in 6 runs and never called;
  cost difference a cent or two.
- **IDE cases: better on one criterion, turns (-30%)**; precision 18/18 vs 17/18
  is one run, not significant; cost and wall time equal.
- **One clear win: rename.** `rename_refactoring` replaces ~25 grep/read/edit
  steps with one call: 40% cheaper, 4x fewer turns, 35% faster.
- **One loss: change signature.** IntelliJ's MCP tools have no change-signature
  refactoring, so the agent edits by hand anyway and the IDE calls only add cost.

**On a clean, mid-size, single-module library, the IDE pays off for refactorings
it performs in one step, and is roughly neutral for navigation**: a capable model
with grep and a compiler resolves overloads and call chains from the text about
as well. That is narrower than the original thesis.

## Limitations and next steps

- One project, and a grep-friendly one: explicit types next to call sites, few
  interfaces, a 10-second Maven build that makes "edit, compile, fix" cheap.
  Untested: large multi-module legacy code (e.g. Spring with dependency
  injection), where grep hits explode and builds take minutes.
- 3 repetitions per task; differences of one run are noise.
- IntelliJ's MCP `rename_refactoring` can't pick an overload (it takes only a
  file and a name and renames the first match), so rename tasks must avoid
  overloaded names.

Found and fixed in the proxy along the way, because they made IDE runs fail or
mislead: tool definitions of all 62 tools (~25.7k tokens per request -> ~1.5k,
#14 #16), calls failing with several projects open (#15), and call trees cut by
the depth limit looking complete (#17).
