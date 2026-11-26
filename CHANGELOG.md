# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.3] - 2026-11-26

### Added
- **Savings meter now reports real savings** (`meter.py`, `_baseline_from_rows`): graph rows carry project-relative paths, so the naive baseline is now resolved against the indexed project roots. `memory_savings` reports actual `saved_bytes`/`saved_tokens_est` instead of zero.
- **`source` provenance on writes** (`remember_fact`, `add_memory`): optional `source` argument stored in fact metadata, closing the "without provenance" finding from `memory_audit`.
- **MCP prompts**: `explore_codebase`, `recall_context`, `audit_memory` exposed as slash-command entry points for clients that support MCP prompts.

## [0.1.2] - 2026-11-25

### Added
- **Savings meter now reports real savings** (`meter.py`, `_baseline_from_rows`): graph rows carry project-relative paths, so the naive baseline is now resolved against the indexed project roots. `memory_savings` reports actual `saved_bytes`/`saved_tokens_est` instead of zero.
- **`source` provenance on writes** (`remember_fact`, `add_memory`): optional `source` argument stored in fact metadata, closing the "without provenance" finding from `memory_audit`.
- **MCP prompts**: `explore_codebase`, `recall_context`, `audit_memory` exposed as slash-command entry points for clients that support MCP prompts.

## [0.1.1] - 2026-11-23

### Added
- **Spaced graph refresh** (`watchdog.py`, `CODEBASE_MEMORY_REFRESH_SPACING_SECONDS` default 15 s): scheduled re-indexing now pauses between targets instead of rebuilding every repo back-to-back (~75 s of CPU in one burst across six projects). Set to `0` to restore the old behaviour. Documented in the README env table.
- **Objective stated in the README**: cut the tokens AI coding agents spend re-discovering a codebase (cold question vs warm question). If a future change doesn't serve that goal, it's scope creep.
- **Savings meter** (`meter.py`, `memory_savings` tool): every graph-backed tool records response bytes and the naive-load baseline (bytes of the source files whose symbols the call returned). `memory_savings` reports per-tool counters with running totals and estimated tokens saved (~bytes/4). Counters persist across restarts in `$MEM0_DIR/memory_savings.json`. Fact fetches/writes count calls and bytes only.
- **Coverage-aware recall** (`recall`): recall now returns a `coverage` block — the graph's own `index_status` note (capped) plus a conservative `refresh_may_help` boolean hint derived from it. No thresholds; agents see the health data and judge.
- **Watchdog crash-loop breaker** (`watchdog.py`, `AMADEUS_WATCHDOG_CRASH_WINDOW` default 600 s, `AMADEUS_WATCHDOG_CRASH_LIMIT` default 5): every restart now logs the exit code of the last child, and supervision of a service that dies more than the limit inside the window stops loudly instead of thrashing forever — each restart costs 8–27 s of cold graph warm-up in practice. README documents the new env vars.
- **Guard test** (`tests/test_objective.py`): monkeypatched graph + deterministic fixture facts assert recall returns bounded bytes, `coverage` propagates, and the savings ledger increments on recall.

### Fixed
- **Crash visibility** (`watchdog.py`): on a child death the watchdog now tails `unified_memory_server.log` into its own log (40 lines) before restarting — losing the stack trace cost us this debugging. Exit code + tail are always in `unified_memory_watchdog.log`.
- **Stale-symbol marking in recall** (`server.py`): every *linked* fact (fact ↔ symbol hit in this recall) is re-resolved against the graph with a bounded budget (max 3 resolutions per recall). Facts whose symbols vanished from the graph since storage are marked `stale_symbol: true`, and the dump carries a `stale_linked_facts` count.
- **REST facade hardened** (`server.py`): the three plain-HTTP routes (`GET/POST /facts`, `/health`) now wrap store access and input validation in explicit 400/500 JSON responses instead of raising raw exceptions (the ASGI 500 cascade logged as `TypeError: 'dict' object is not callable` was a pre-restart deployment artifact, but it cost a debugging session — never again).
- **Index hygiene** (`server.py`): `index_repository` now defaults to `fast` mode (the binary's own filtered mode) when the caller doesn't ask; pass `mode="full"` explicitly for a full build.

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
