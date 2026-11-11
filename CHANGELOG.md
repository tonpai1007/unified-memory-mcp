# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- Optional supervisor: `python -m amadeus_memory.watchdog` — probes the port,
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
