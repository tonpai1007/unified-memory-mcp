# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Caller-aware default project** (portability): the MCP server's own CWD never changes, so the default project for graph/fact calls was frozen at boot and every other codebase needed an explicit `project=`. Now: `remember_fact` and `recall` read the caller's working directory from the MCP `_meta` (`ai.opencode/directory` best-effort) and match it against the indexed projects (exact root path first, then the `C-<path>` name suffix); an explicit `project=` argument still wins, and the env override still beats everything. `graph_projects` is a new discovery tool — lists every indexed project with its root path and names the deployment default, so working on any codebase is: list → pass `project=<name>`.
- Also fixed a latent bug found while touching this path: `fact_freshness` and `fact_impact` called the async `_default_project()` without `await`, so with no explicit `project` and no fact scope the symbol resolution received a coroutine object instead of a project name.

### Added
- **Write-side dedup** (caveat #6): the three write paths — MCP `add_memory`, MCP `remember_fact` and REST `POST /facts` — now check for a near-identical fact (token Jaccard ≥ 0.82, the same threshold `memory_dedupe`/`memory_audit` use) in the same user scope **before** inserting. On a hit nothing is written: the tools return `{"stored": null/…, "duplicate_of": {id, similarity, …}}` and the REST endpoint answers `200 {ok: true, duplicate_of: …}` idempotently, so the brain's fire-and-forget write-back cannot pile up duplicate rows when the same event fires twice. Expired facts never block a fresh restatement, and every path takes `dedupe: false` to store anyway. `tests/test_write_dedupe.py` covers all three paths against a faked store (no real store, no graph binary).

### Added
- **System vitals** (`system_vitals`): one call that answers "is the whole system OK?" — brain status and uptime, scheduler job count, open circuit breakers, dead-letter queue depth, vault reachability, response-cache hit rate. Reads the brain's HTTP API (`AMADEUS_BRAIN_URL`, default `http://127.0.0.1:8000`) and reports `unreachable` instead of failing when the brain is down.
- **Memory hygiene** tools, because the fact store accumulates untidy rows:
  - `memory_audit` — scope coverage per project, unscoped facts, expired facts, facts without provenance or symbol links, and near-duplicate pairs (token Jaccard).
  - `memory_scope_facts` — assigns a project scope to unscoped facts (metadata only, text is never rewritten); without an explicit project the scope is derived from the fact's linked symbols (the graph's qualified-name prefix). Dry run by default.
  - `memory_dedupe` — removes near-duplicate rows (token Jaccard ≥ threshold), keeping the OLDEST copy so the original timestamp survives. Dry run by default.
  - `memory_purge_expired` — deletes facts whose TTL has passed. Dry run by default.
- `store.mem0_rows` / `store.expiry_of` / `store.call` are now the single implementations of the row/TTL/call helpers (previously duplicated in the server module).

## [0.1.0] - 2026-11-11

### Added
- Unified MCP server exposing **both** memory surfaces over one process:
  - semantic facts (mem0): `add_memory`, `search_memories`, `get_memories`,
    `get_memory`, `update_memory`, `delete_memory`, `delete_all_memories`
  - a code knowledge graph (codebase-memory, optional external dependency):
    `search_graph`, `trace_path`, `get_code_snippet`, `query_graph`,
    `get_architecture`, `check_index_coverage`, `detect_changes`,
    `list_projects`, `index_status`, `index_repository`, `delete_project`,
    plus a generic `codebase_memory` escape hatch
- Cross-linked recall (the reason for merging the two stores):
  - `remember_fact(text, symbols=[…])` — resolves each symbol against the
    graph and stores `{qualified_name, file, kind}` in the fact's metadata
  - `recall(query)` — one fused call over facts **and** code symbols, with
    `facts_linked_to_code` naming the overlap
  - `facts_for_symbol(symbol)` — the reverse index
  - `fact_freshness(memory_id)` — re-validates a fact's symbols (existence,
    file moves) and its TTL
  - `fact_impact(memory_id)` — callers/callees of every linked symbol, plus
    optionally the current working-tree impact
- **Project scoping** — facts carry `metadata.project`; reads filter by it
  client-side (upstream mem0 filters only on `user_id`/`agent_id`).
- **TTL / expiry** — `stale_after_days` or an absolute `stale_after`; searches
  and recalls annotate each fact with `ttl {expires_at, expired, days_left}`,
  and `expired_facts` lists expired and expiring-soon facts.
- **Session provenance** — the calling session id is captured automatically
  from the MCP request `_meta` (`ai.opencode/sessionID`).
- `facts_by_project` — per-project inventory of the fact store.
- Optional supervisor: `python -m unified_memory.watchdog` — probes the port,
  restarts the server with a boot grace period, and can re-index the graph on
  a schedule (`CODEBASE_MEMORY_REFRESH_MINUTES`).

### Notes
- Transport: Streamable HTTP by default, in **stateless** mode so a server
  restart does not invalidate client session ids; `--stdio` for stdio clients.
- The fact store uses 512-dim FastEmbed embeddings; `OPENAI_API_KEY` is
  deliberately ignored so the embedder cannot flip and corrupt the collection.
- The code graph child is kept warm in a dedicated thread with its own event
  loop: per-call CLI spawning reloads the whole index (7.4–8.5 s measured)
  versus tens of milliseconds warm.
