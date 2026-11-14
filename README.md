# unified-memory-mcp

**One MCP server for two memory surfaces: semantic facts (mem0) and a code knowledge graph (codebase-memory) — plus the join between them.**

## Objective

**Cut the tokens AI coding agents spend re-discovering a codebase.**

Every LLM agent session without persistent memory of your code starts cold: it greps for symbols, re-reads whole directories, dumps entire files into the context window — and pays that same price again in every next session, on every question, from scratch. That is not a tool problem; it is a memory deficit.

This package is the fix: one shared, always-on memory of what an agent learned *about the code* (the graph — symbols, calls, structure) and *about the decisions* (the facts — what you did last week, why, and which code it touched). Two halves, fused into one callable surface, available to every agent on the machine through one URL.

Working definitions:

- **Cold question** — "what does this function do / who calls it / what did we decide about this module?" without memory: symbol lookup + file dumps + greps, repeated every session.
- **Warm question** — same question asked of the memory: one tool call, a few hundred tokens, correct answer *and the code it lives in*.

Success test, honest, measurable, and deliberately narrow: an agent asking any graph or recall question should never again answer it by re-reading files it already paid for. If memory is warm, it answers from memory; if it isn't, it reads once and writes the answer into memory so nobody pays for it twice.

Everything else in this repo — the warm graph child, scoped facts, write-side dedup, fused `recall`, TTL — is the machinery to make that one goal hold under real use. If any future change doesn't serve the objective above, it's scope creep; leave it out.

Two MCP servers used to live behind stdio transport. MCP stdio means *the client spawns the server*, so every editor session and every project directory got its own copy. On one machine that was 13+ processes and ~1 GB of RAM for the same two servers — and the fact store's embedded Qdrant collection is single-process locked, so the extra copies were contending for the same lock instead of adding capacity.

This package serves both surfaces from **one process** over Streamable HTTP, and adds what merging makes possible: facts can be linked to the symbols they describe, so recall returns the answer *and* the code.

```
┌── opencode / claude / cursor …─┐
        memory_add_memory      memory_search_graph
        memory_remember_fact   memory_trace_path
        memory_recall          memory_fact_freshness
        memory_facts_for_symbol memory_fact_impact
        memory_facts_by_project memory_expired_facts
        memory_index_repository …           ← one shared process
└───────────────┬────────────────────────────┘
                │ 127.0.0.1:8777/mcp  (stateless HTTP)
     ┌──────────┴───────────┐
     │  mem0 (facts)        │  codebase-memory (code graph)
     │  Qdrant + history.db │  one warm child, index in RAM
     └──────────────────────┘
```

## Why the join matters

| Tool | Question it answers |
|---|---|
| `system_vitals` | *Is the whole system OK?* — brain health and uptime, scheduler jobs, open circuit breakers, dead-letter queue, vault reachability, cache hit rate, in one call |
| `memory_audit` | *Is my memory trustworthy?* — scope coverage, unscoped facts, expired facts, missing provenance, near-duplicates |
| `memory_scope_facts` | *Stop one project's facts polluting another's recalls* — assigns a project scope (metadata only, dry run by default) |
| `memory_dedupe` | *Remove the same fact stored twice* — deletes near-duplicate rows, keeping the oldest (dry run by default) |
| `memory_purge_expired` | *Clean up facts that decayed* — deletes TTL-expired facts (dry run by default) |
| `remember_fact(text, symbols=[...])` | *Store a fact and pin it to the code it describes* - the symbol is resolved to its qualified name + file and kept in the fact's metadata. Write-side dedup: a near-identical fact (token Jaccard >= 0.82) in the same scope returns the existing id (`duplicate_of`) instead of inserting a second row; `dedupe: false` stores anyway |
| `recall(query)` | *Give me the answer and the code* — one fused call over facts **and** graph symbols, with `facts_linked_to_code` calling out the overlap |
| `facts_for_symbol(symbol)` | *What do we know about this function?* — the reverse index |
| `fact_freshness(memory_id)` | *Is this old fact still true?* — re-checks that every linked symbol still exists, that no file moved, and that the fact's TTL has not passed |
| `fact_impact(memory_id)` | *What breaks if I touch this?* — callers/callees of every linked symbol, optionally plus the impact of the current working-tree changes |

Facts can also be **scoped to a project** (`project` on write and read) so a finding from one project never pollutes another project's recall, **tagged with their session** (captured automatically from the MCP request `_meta`), and given a **TTL** (`stale_after_days` / `stale_after`) so facts about code decay instead of being trusted forever.


**Any codebase, not just the deployment default.** The code graph indexes every project it was pointed at; `graph_projects` lists them with their root paths. `remember_fact` / `recall` pick up the calling session's working directory from the MCP `_meta` and match it to an indexed project automatically; for every other graph tool pass `project=<name>` explicitly (or set `CODEBASE_MEMORY_PROJECT` to pin one deployment-wide).

## Install

```bash
pip install unified-memory-mcp
```

Straight from the tagged GitHub release (no PyPI account needed):

```bash
pip install "unified-memory-mcp @ git+https://github.com/tonpai1007/unified-memory-mcp@v0.1.0"
```

The fact store works standalone (the package builds the same store with
mem0ai directly). To instead reuse the **upstream local-store** mem0 MCP
module verbatim — identical store layout, `history.db` bookkeeping and embedder
config — install it from git:

```bash
pip install "git+https://github.com/zfy258/mem0-mcp-server.git"
```

> Note: the PyPI distribution `mem0-mcp-server` (0.2.x) is a **hosted-API**
> client with a different contract; this package detects that and falls back
> to the direct mem0ai path.

The code graph is an **optional external dependency** — it is not bundled:

```bash
# see https://github.com/DeusData/codebase-memory-mcp for install options
codebase-memory-mcp --help
```

Without it, every fact tool still works; the graph tools return a clear error.

## Run

```bash
unified-memory-mcp                 # Streamable HTTP on 127.0.0.1:8777/mcp
unified-memory-mcp --stdio         # stdio transport, for clients that need it
unified-memory-mcp --port 9000     # custom port
```

Keep it alive (it is a shared service, so something should supervise it):

```bash
python -m unified_memory.watchdog
```

The watchdog probes the port every 45 s, restarts the server when it dies (with a grace period, so a booting server is not killed again), and can re-index the graph on a schedule. Each restart logs the exit code of the last child so a dying server stops being invisible; if the same service dies more than `AMADEUS_WATCHDOG_CRASH_LIMIT` times inside `AMADEUS_WATCHDOG_CRASH_WINDOW` seconds, supervision of that service stops loudly instead of thrashing forever (each restart costs 8–27 s of cold graph warm-up):

```bash
export CODEBASE_MEMORY_REFRESH_MINUTES=30
export CODEBASE_MEMORY_REFRESH_TARGETS="MyProject=/path/to/repo"
```

## Client configuration

OpenCode (`~/.config/opencode/opencode.jsonc`):

```jsonc
{
  "mcp": {
    "servers": {
      "memory": {
        "type": "remote",
        "url": "http://127.0.0.1:8777/mcp"
      }
    }
  }
}
```

Claude Desktop / Cursor / any stdio client:

```json
{
  "mcpServers": {
    "memory": {
      "command": "unified-memory-mcp",
      "args": ["--stdio"]
    }
  }
}
```

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `MEM0_DIR` | `~/.amadeus-memory` | fact store root (Qdrant + `history.db`) |
| `MEM0_HOST` / `MEM0_PORT` | `127.0.0.1` / `8777` | HTTP bind address |
| `CODEBASE_MEMORY_EXE` | auto-detected | path to the codebase-memory binary |
| `CODEBASE_MEMORY_PROJECT` | auto (CWD's project) | default project for graph queries |
| `CODEBASE_MEMORY_REFRESH_MINUTES` | `0` (off) | scheduled graph refresh interval |
| `AMADEUS_WATCHDOG_INTERVAL` | `45` | how often the watchdog probes the port |
| `AMADEUS_WATCHDOG_GRACE_SECONDS` | `120` | grace period for a just-started service to bind |
| `AMADEUS_WATCHDOG_CRASH_WINDOW` | `600` | crash-loop window in seconds |
| `AMADEUS_WATCHDOG_CRASH_LIMIT` | `5` | restarts allowed per window before supervision stops |
| `CODEBASE_MEMORY_REFRESH_TARGETS` | – | `name=/path` pairs to re-index |
| `OPENAI_API_KEY` | ignored | deliberately unset: the store is 512-dim FastEmbed and must not flip to OpenAI embeddings |

## Design notes

* **Stateless HTTP.** Each POST is self-contained, so a server restart does not invalidate the MCP session ids held by connected clients. With session state enabled, clients keep posting a stale id and get HTTP 400 with no auto-reconnect.
* **One warm graph child.** The graph child lives in a dedicated thread with its own event loop. It cannot be owned by a request handler — anyio's `stdio_client` raises *"Attempted to exit a cancel scope in a different task than it was entered in"* — and stateless HTTP makes every request its own task. Spawning the CLI per call instead reloads the whole index every time (7.4–8.5 s measured, versus tens of milliseconds warm).
* **Client-side scoping.** Upstream mem0 filters only on `user_id`/`agent_id`, so project scoping and TTL flags are applied here (over-fetch, then filter) rather than in the store.

## Releasing

1. Bump `version` in `pyproject.toml` and add a `CHANGELOG.md` entry.
2. Tag and push: `git tag -a vX.Y.Z -m "…" && git push origin vX.Y.Z`
   → `release.yml` builds, runs `twine check`, creates the GitHub release and
   publishes to PyPI.
3. Publishing to PyPI needs a publisher registered for this repository:
   - **Trusted publishing (recommended)** — on PyPI: *Account → Publishing → Add
     a new publisher → GitHub*, owner `tonpai1007`, repository
     `unified-memory-mcp`, workflow `release.yml`. The workflow requests an
     OIDC token (`id-token: write`) and PyPI exchanges it.
   - **API token** — create a token on PyPI and add it as the repository
     secret `PYPI_API_TOKEN`; the workflow uses it when present.

Both paths work with the same workflow; with neither configured the GitHub
release is still created and only the PyPI step fails.

## Licence

Apache-2.0 — see `LICENSE`.

This package builds on [mem0](https://github.com/mem0ai/mem0) (Apache-2.0) and can optionally use the upstream [mem0-mcp-server](https://github.com/zfy258/mem0-mcp-server) module (Apache-2.0) verbatim; see `NOTICE`. The code graph comes from the separate [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) project, which is an optional runtime dependency and is **not** redistributed here.
