# idea-mcp-proxy

A reverse proxy for the JetBrains IDE's built-in MCP server. It sits between
an MCP client (Claude Code) and the real IDE server, passes almost every
tool through unchanged, and locally trims the one tool whose default output
is disproportionately verbose: `analyze_calls`.

**Status: wired into Claude Code's actual config and running.**
`~/.claude.json`'s `mcpServers.idea` now points at this proxy
(`http://127.0.0.1:8765/stream`); the real server is still registered
under `idea-raw` (`http://127.0.0.1:64342/stream`) as a fallback. Read
"Current status" below for what that does and doesn't cover.

## Why this exists

For an AI agent like Claude Intellij Idea MCP service call `analyze_calls` (IDE call-hierarchy / find-usages) is precise in a way
`grep` fundamentally cannot be -- it resolves real call relationships
instead of text co-occurrence, so it can correctly say "zero callers" or
disambiguate overloaded/shadowed names. But its raw output repeats every
node's *entire ancestor path* as a string on every line, and renders every
test-file caller as its own line even when dozens share an identical
one-hop shape. On a real query against a mid-size Java project (`kwg-backend`,
`SRCByteReader` class, call hierarchy at depth 5) this cost:

- **raw**: ~48,600 characters, all 75 callers rendered flat, ancestry
  duplicated per node
- **proxied, `mode=summary` (default)**: **571 characters** -- direct
  callers listed, everything else grouped by file/scope, test callers
  (73 of them) collapsed to a count
- **proxied, `mode=tree, sourceScope=production`**: 1,836 characters --
  full tree, but test noise removed and ancestry shown via indentation
  instead of a repeated string per node

A second real-world query (`SRCByteReader.getInstance`, a widely-used
factory method) exposed a real bug in the first version of this summary:
43 of 51 total callers were *direct* -- one hop from the root, not several
levels deep -- and the summarizer only collapsed leaves several levels
down, so it listed all 43 individually (6,758 chars vs. 17,232 raw, only a
61% reduction, most of it wasted on a wall of near-identical direct-caller
lines). Fixed by splitting direct callers into "gateway" nodes (have
further callers of their own -- worth naming individually) vs. leaf
callers (fold into the same grouped/collapsed accounting as every other
leaf, regardless of depth). After the fix: 866 chars, a 95% reduction.
See `tests/test_summarize.py::test_render_summary_collapses_flat_direct_fanout`.

That's roughly a 96-99% reduction depending on mode, achieved without
losing the precision that made the tool worth using over grep in the first
place -- the filtering/aggregation happens in plain deterministic code,
never by asking an LLM to read past the noise.

See the conversation that led here for the full reasoning; the short
version: verbose tool output costs LLM context tokens whether or not the
information in it was needed, and this class of problem (mechanical
transform of a codebase-tool's response before it reaches a model) is a
better fit for a proxy than for prompting.

## How the real IDE server works (found by probing, not documented)

The JetBrains IDE MCP server is **not** a subprocess Claude Code spawns. It's
an HTTP server the IDE plugin itself runs, listening on
`http://127.0.0.1:64342/stream`, found in Claude Code's global config at
`~/.claude.json`:

```json
"mcpServers": {
  "idea": { "url": "http://127.0.0.1:64342/stream", "type": "http" }
}
```

It speaks MCP's Streamable HTTP transport (`mcp` Python SDK 2.0.0,
`streamable_http_client` / `ClientSession`). It's already running whenever
IntelliJ IDEA is open with the plugin active -- independent of any Claude
Code session lifecycle. That makes it straightforward to proxy: just another
local HTTP endpoint, no subprocess management needed.

### The `analyze_calls` raw format

Not JSON -- a pre-rendered text blob using box-drawing characters:

```
└─ + SRCByteReader(FileChannel, Charset) [filePath="...", treePath=[]]
   └─ + createReader(Path, Charset) [filePath="...", treePath=["com...createReader(Path, Charset)"]]
      ├─ + getAsciiInstance(Path) [filePath="...", treePath=["...","...getAsciiInstance(Path)"]]
```

**Key discovery**: `treePath` is each node's full ancestor signature chain,
*including its own signature as the last element*. That means the tree
structure can be reconstructed purely from `treePath` prefix relationships
-- a node's parent is whichever node's own `treePath` equals this node's
`treePath[:-1]`. No need to parse the indentation or `├─`/`└─`/`│`
connectors for structure at all; they're cosmetic. See
`idea_mcp_proxy/tree_parser.py` for the full explanation.

**Gotcha that cost real debugging time**: both the display `name` and the
signatures inside `treePath` can contain literal `[`/`]` characters --
Java array and generic syntax, e.g. `main(String[])` or
`dispatch(String, Map<String, String>)`. An earlier regex that excluded
bracket characters to find the name/treePath boundary broke silently on
any call chain deep enough to reach a method with array/generic params
(reproduced with `depth=5` on `IkdCliTool.main(String[])`, four levels
below the query root). The parser now anchors on the fixed literal
`[filePath="` marker instead of excluding brackets -- see the comment in
`tree_parser.py` and the regression test in `tests/test_tree_parser.py`.
**If you touch the parsing regex again, run that test.**

Other tools were checked too: `search_symbol` already returns compact JSON
(not a rendered tree), so it isn't wrapped. `tools/list` returned 59 tools
total on the probed instance (IntelliJ IDEA MCP Server 2026.2.0.1).

## Architecture

```
Claude Code -> http://127.0.0.1:8765/stream (this proxy) -> http://127.0.0.1:64342/stream (real IDE server)
```

- `idea_mcp_proxy/upstream.py` -- long-lived connection to the real server,
  held open for the proxy process's lifetime via the MCP `Server`'s
  lifespan hook (not reconnected per-request).
- `idea_mcp_proxy/server.py` -- the proxy itself. `on_list_tools` mirrors
  the real server's tool list, extending only `analyze_calls`'s schema.
  `on_call_tool` passes every tool through unchanged except `analyze_calls`,
  which routes to the wrapper.
- `idea_mcp_proxy/tree_parser.py` -- parses the raw `analyze_calls` text
  into a `CallNode` tree via the `treePath`-prefix approach above. Raises
  `TreeParseError` on anything unexpected.
- `idea_mcp_proxy/summarize.py` -- turns a `CallNode` tree into compact
  output: production/test classification (path heuristic, overridable),
  `paths` glob filtering (mirrors `search_symbol`'s existing param),
  `sourceScope` filtering, and two render modes (`summary` grouped/collapsed,
  `tree` full but deduplicated).
- `idea_mcp_proxy/analyze_calls_tool.py` -- wires the above together:
  extends the tool schema with `paths`/`sourceScope`/`mode`, calls the real
  server, parses, filters, re-serializes. **Falls back to the raw upstream
  response, untouched, if parsing fails for any reason** (logged as a
  warning) -- a format change upstream should degrade to "no worse than
  today", never to a corrupted or silently wrong tree.

New `analyze_calls` parameters (on top of the real tool's own schema):

| param | values | effect |
|---|---|---|
| `paths` | glob list, `!` for excludes | same semantics as `search_symbol`'s `paths`, applied locally after fetching |
| `sourceScope` | `production` \| `test` \| `all` (default) | keeps only callers whose file matches (heuristic: `/test/` path segment or `*Test`/`*Tests`/`*IT`/`*Spec` filename) |
| `mode` | `summary` (default) \| `tree` | `summary`: direct callers listed, rest grouped by file, test callers collapsed above a threshold. `tree`: full re-serialized tree, ancestry via indentation not a repeated string |

### Tool description enrichment

`idea_mcp_proxy/tool_descriptions.py` appends a short "[disambiguation]"
note to 8 tools whose purpose overlaps enough to cause wrong picks by an
agent choosing between them at discovery time -- the `search_*` family
especially (`search_file` is paths-only, `search_text`/`search_regex` are
unindexed text matching with false-positive risk, `search_symbol` is the
index-based one and should usually be preferred), plus
`get_symbol_info`/`analyze_calls`/`rename_refactoring`/`generate_psi_tree`
cross-references. Applied in `server.py`'s `_on_list_tools` via
`_annotate_tool` -- description only, schema/name/behavior untouched. This
is deliberately targeted at genuine ambiguity rather than applied
uniformly to all ~59 tools: padding every description adds noise to
keyword-based tool search without helping anyone pick correctly. Add more
entries to `DISAMBIGUATION_NOTES` only where real confusion shows up.

## Current status

**Done:**
- Passthrough proxy core, tested against all 59 real tools (spot-checked
  `search_symbol` explicitly; others go through the same unmodified path)
- `analyze_calls` wrapper with `paths`/`sourceScope`/`mode`, validated
  end-to-end against a live IntelliJ IDEA instance on a real project
  (`kwg-backend`, `SRCByteReader` class)
- Regression tests for the tree parser (`tests/test_tree_parser.py`),
  using real captured server output as fixtures (`tests/fixtures/`)
- Bug found and fixed during testing: bracket characters in Java
  array/generic signatures broke the original parsing regex (see above)
- Wired into `~/.claude.json`: `mcpServers.idea` points at the proxy,
  `mcpServers.idea-raw` keeps the real server directly reachable as
  fallback. Started detached (`nohup ... & disown`) so it survives the
  shell that launched it, PID tracked in `/tmp/proxy_server.pid` for
  manual restart if needed.
- Description enrichment for 8 tools whose purpose overlaps enough to
  cause wrong picks at discovery time (`search_file`/`search_text`/
  `search_regex`/`search_symbol` especially) -- see "Tool description
  enrichment" above.

**Not done:**
- No process supervision -- started manually, nothing restarts it if it
  or the IDE crashes, and it does not survive a reboot. A dead proxy
  breaks every `idea` tool call for every Claude Code session until
  someone notices and restarts it (see "Running it" below) or flips
  `mcpServers.idea` back to `idea-raw` in `~/.claude.json`. Worth adding a
  scheduled task / startup entry if this proves useful enough to keep.
- Because `~/.claude.json` is read at session start, this only takes
  effect for **new** Claude Code sessions -- a session already running
  when the config changed keeps its original direct connection.
- Only `analyze_calls` is wrapped. `get_file_problems`, `search_symbol`
  etc. were spot-checked as "probably fine as-is" but not measured under
  real verbose-output conditions the way `analyze_calls` was.
  `generate_psi_tree` was flagged in the original discussion as
  potentially worth wrapping too but hasn't been probed.
  The `"... and N more"` truncation marker (mentioned in `analyze_calls`'s
  own tool description for results exceeding `maxChildren`) is not
  specifically handled by the parser -- current regex will raise
  `TreeParseError` on it, which safely falls back to raw passthrough, but
  a real truncated response has not been captured/tested.
- No empirical measurement of *overall session* token savings, only raw
  character counts on one query. Real validation would mean logging
  before/after token counts on several representative legacy-codebase
  tasks (this was flagged as the honest way to confirm the efficiency
  estimate discussed before building this).
- `sourceScope` test/production heuristic is Maven/Gradle-convention-based
  and not verified against other project layouts.

## Running it

```bash
cd idea-mcp-proxy
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Start the proxy (requires IntelliJ IDEA running with the MCP plugin active,
i.e. something already listening on `127.0.0.1:64342`):

```bash
./.venv/Scripts/python.exe -m idea_mcp_proxy.server
```

Serves on `http://127.0.0.1:8765/stream`.

## Testing

```bash
./.venv/Scripts/python.exe -m pytest tests/ -v
```

Two dev scripts, both write to `scripts/*.txt` (git-ignored) rather than
printing directly -- the raw `analyze_calls` text contains Unicode
box-drawing characters that crash on Windows' default cp1252 console
encoding:

- `scripts/probe_real_server.py` -- talks to the **real** IDE server
  directly (bypasses the proxy). Useful for re-capturing ground truth if
  the IDE plugin version changes and something breaks.
- `scripts/test_proxy_client.py` -- talks to the **proxy**, exercises all
  three `analyze_calls` modes plus a passthrough sanity check
  (`search_symbol`). This is the script that caught the bracket-parsing
  bug -- if you change the parser or summarizer, rerun it and eyeball the
  `[size: N chars]` lines and actual content, not just that it doesn't
  crash.

## Investigated but not built: "what JARs does class X need"

A real user query -- *"identify all jar archives required to be imported
for the class BatchInfrastructureConfiguration"* -- fell back to brute
force: `Grep` for the class name, `Read` on the 2 matching files (the
class exists in **two different modules**, `kwg-backend-ikd` and
`kwg-backend-fusion` -- real ambiguity), `Glob` for `pom.xml`, then `Read`
on the **root** `pom.xml`. No `idea` MCP tool was touched at all.

This is a fundamentally different question than call-hierarchy verbosity
-- it's not about one tool's output being too long, it's about no single
tool answering the question, so investigating it meant checking what's
actually available before proposing anything.

**What's available, and why it doesn't directly answer this:**
- `get_project_modules` -- flat list of modules (name + type only, e.g.
  `{"name":"kwg-backend-ikd","type":"JAVA_MODULE"}`). Doesn't say what's
  *in* a module or what a module depends on.
- `get_project_dependencies` -- returns **every** dependency across the
  **entire multi-module project** in one flat list (~7,100 characters,
  100+ jars for `kwg-backend`), not scoped to one module or class. Can't
  tell you what a specific file needs.
- `get_repositories` -- VCS roots (git), unrelated to build/module
  structure.
- Between them: nothing maps "this class's imports -> the JARs that
  provide them." That gap is real, not a discoverability problem fixable
  with a better tool description.

**The correctness problem with the brute-force path, not just the cost
one:** reading the *root* `pom.xml` in a 4-module Maven project
(confirmed via `get_project_modules`: `kwg-backend`,
`kwg-backend-shared`, `kwg-backend-fusion`, `kwg-backend-ikd`) almost
certainly misses the actual dependency -- Maven multi-module projects
declare dependencies per-module, and Spring Boot's parent POM manages
versions via a BOM invisible as plain text in either file. The
brute-force approach wasn't just expensive, it was likely heading toward
an **incomplete or wrong** answer.

**Validated mechanism for a real fix:** `search_symbol` with
`include_external=true` resolves an import straight to its source JAR.
Confirmed against a real import in this file
(`org.springframework.batch.core.configuration.support.JdbcDefaultBatchConfiguration`):
resolves to
`...\.m2\repository\org\springframework\batch\spring-batch-core\6.0.2\spring-batch-core-6.0.2-sources.jar!\...`.
That's ground truth from the IDE's actual resolved classpath, not text
inference from XML.

**Why this isn't built as a documented multi-step workflow:** the naive
chain -- `search_symbol` (locate class, disambiguate) -> `read_file` (get
imports) -> one `search_symbol(include_external=true)` per external
import -- comes to **~7 round trips** for this file (5 external imports
out of 7 total, after dropping the 1 JDK and 1 project-internal import).
That's not a clear win over the ~6 round trips brute force actually used;
each round trip carries its own tool-call envelope overhead regardless of
payload size, and more round trips also means more reasoning turns
deciding what to call next. Documenting "call these tools in this order"
for an agent to follow itself would not reliably beat brute force.

**What would actually make it efficient:** the same principle as
`analyze_calls` -- a single composite proxy tool (e.g.
`list_class_dependencies`) that does all ~7 internal IDE calls itself via
direct Python -> localhost HTTP (cheap, zero LLM-context cost, tens of ms
each), and returns the agent exactly one compact result: a deduplicated
JAR list, plus disambiguation candidates if the class name matches
multiple files. The fan-out has to be hidden in code, not exposed as
agent-visible tool calls, or it doesn't save anything.

**Design sketch, if built later:**
1. `search_symbol` to resolve the class name; return all candidates if
   ambiguous (mirrors how `analyze_calls` already surfaces ambiguous
   `symbolFqn`s) rather than silently picking one.
2. `read_file` the resolved file, parse `import` lines.
3. Drop `java.*`/`javax.*` (JDK) and same-project-prefix imports (not
   external dependencies).
4. Resolve each remaining import via `search_symbol(include_external=true)`;
   extract the JAR name from the archive-entry-style path.
5. Return the deduplicated JAR list.

Not built yet -- documented here so a future agent (or session) doesn't
have to re-derive this investigation from scratch, and so "should we
build this" is a decision made with real numbers already in hand rather
than from scratch.

## Next steps

1. Decide whether/how to wire into `~/.claude.json` (global -- ask the
   user first; consider keeping the real server registered under a second
   name for fallback).
2. Add the `"... and N more"` truncation case to the parser and test it
   against a query that actually exceeds `maxChildren` (none of the
   `SRCByteReader` queries used so far did -- 75 callers is under the
   default 50-per-node limit only because they're spread across multiple
   parent nodes; a flatter high-fan-out symbol would trigger it).
3. If real usage shows other tools are also verbose in practice, extend
   the same wrap-and-summarize pattern to them -- the passthrough
   architecture makes adding one more wrapped tool cheap.
4. Measure actual session-level token impact empirically rather than
   relying on the single-query character-count comparison above.
