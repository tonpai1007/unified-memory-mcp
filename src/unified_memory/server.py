"""The unified memory MCP server.

Serves two memory surfaces from one process:

* **semantic facts** (mem0) â€” see :mod:`unified_memory.store`
* **a code knowledge graph** (codebase-memory) â€” see :mod:`unified_memory.graph`

and joins them: facts are linked to the symbols they describe, so ``recall``
returns the answer *and* the code, ``facts_for_symbol`` answers "what do we
know about this function?", and ``fact_freshness`` re-validates a fact against
the code instead of trusting it forever.

Transport
---------
Streamable HTTP (default) on ``127.0.0.1:8777/mcp``, in **stateless** mode:
each POST is self-contained, so a server restart does not invalidate the MCP
session ids held by connected clients. ``--stdio`` serves the same tools over
stdio for clients that require it.

Environment
-----------
MEM0_DIR                   fact store root (default ``~/.amadeus-memory``)
MEM0_PORT                  HTTP port (default 8777)
MEM0_HOST                  HTTP host (default 127.0.0.1)
CODEBASE_MEMORY_EXE        path to the codebase-memory binary (optional)
CODEBASE_MEMORY_PROJECT    default project for graph queries
OPENAI_API_KEY             intentionally ignored (keeps the 512-dim embedder)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import graph as graph_mod
from . import store as store_mod

INSTRUCTIONS = (
    "Unified memory surface: semantic facts (mem0) AND a code knowledge graph. "
    "Facts answer 'what did we learn/decide'; the graph answers 'how does this code connect'. "
    "Use remember_fact to store a fact linked to the symbols it describes, recall for a fused "
    "facts+code answer, fact_freshness to re-validate an old fact, and facts_for_symbol to ask "
    "what is known about a function."
)

mcp = FastMCP(
    "amadeus-memory",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
)

# â”€â”€ defaults â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_default_project_cache: str | None = None


async def _default_project() -> str:
    """Resolve the default project: env override, else the CWD's own project."""
    global _default_project_cache
    override = os.environ.get("CODEBASE_MEMORY_PROJECT")
    if override:
        return override
    if _default_project_cache:
        return _default_project_cache
    # Fall back to the first indexed project (best effort, non-blocking).
    try:
        raw = await graph_mod.call("list_projects", {})
        data = json.loads(raw)
        projects = data.get("projects") or []
        if projects:
            folder = os.path.basename(os.getcwd()).upper()
            for project in projects:
                if str(project.get("name", "")).upper().endswith(folder):
                    _default_project_cache = str(project["name"])
                    return _default_project_cache
            _default_project_cache = str(projects[0]["name"])
    except Exception as exc:
        # The graph may be unavailable (binary not installed) — the fact side
        # must keep working, so this is a soft fallback, not an error.
        print(f"[amadeus-memory] project auto-detect failed: {exc!r}", file=sys.stderr)
    return ""


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _dump(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as exc:  # pragma: no cover
        return json.dumps({"error": f"serialize failed: {exc}"})


def _cb_text(raw: str) -> str:
    """Normalise a graph payload to plain text."""
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, dict):
        for key in ("result", "text", "output"):
            if isinstance(data.get(key), str):
                return data[key]
    return raw


def _session_id_from(ctx: Any) -> str | None:
    """Extract the calling session id from the MCP request _meta.

    OpenCode sends ``ai.opencode/sessionID`` in ``_meta``; capturing it
    automatically means every stored fact records which session produced it.
    """
    try:
        meta = ctx.request_context.meta
    except Exception:
        return None
    if meta is None:
        return None
    extra = getattr(meta, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("ai.opencode/sessionID", "sessionId", "session_id"):
            if extra.get(key):
                return str(extra[key])
    value = getattr(meta, "ai.opencode/sessionID", None)
    return str(value) if value else None


def _mem0_rows(raw: str) -> list[dict[str, Any]]:
    """Row normaliser — see :func:`unified_memory.store.mem0_rows`."""
    return store_mod.mem0_rows(raw)


def _mem0(tool: str, args: dict[str, Any]) -> str:
    try:
        result = store_mod.call_tool(tool, args)
    except Exception as exc:
        return _dump({"error": f"{tool} failed: {exc}"})
    if isinstance(result, dict) and "results" in result:
        results = []
        for row in result.get("results") or []:
            if isinstance(row, dict):
                results.append(
                    {
                        "id": row.get("id"),
                        "memory": row.get("memory"),
                        "score": row.get("score"),
                        "user_id": row.get("user_id"),
                        "metadata": row.get("metadata"),
                    }
                )
            else:
                results.append(row)
        return _dump({"results": results})
    return _dump(result)


def _expiry_of(row: dict[str, Any]) -> dict[str, Any]:
    """TTL state of a fact — see :func:`unified_memory.store.expiry_of`."""
    return store_mod.expiry_of(row)


def _with_expiry(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    expired = 0
    for row in rows:
        state = _expiry_of(row)
        row["ttl"] = state
        if state["expired"]:
            expired += 1
    return rows, expired


def _parse_graph_rows(text: str, limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        head = re.match(r"^(\S+) \(([^)]+)\):\s*$", line)
        if head:
            if current:
                rows.append(current)
            current = {
                "qualified_name": head.group(1),
                "file": head.group(2),
                "name": head.group(1).rsplit(".", 1)[-1],
                "kind": "",
            }
            continue
        detail = re.match(r"^\s+(\S+)\s+(Method|Function|Class|Interface|Route|Module|Variable)\b", line)
        if detail and current is not None:
            current["name"] = detail.group(1)
            current["kind"] = detail.group(2)
    if current:
        rows.append(current)
    return rows[:limit]


def _parse_trace(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"callers": [], "callees": []}
    current_group = ""
    for line in text.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            key = key.strip()
            if key in ("function", "direction"):
                out[key] = value.strip()
            elif key in ("callers_total", "callees_total"):
                with contextlib.suppress(ValueError):
                    out[key] = int(value.strip())
            continue
        if line.startswith("  ") and line.strip():
            parts = line.strip().rsplit(" ", 1)
            name = parts[0]
            hop = parts[1] if len(parts) > 1 else ""
            bucket = "callers" if out.get("direction", "inbound") == "inbound" else "callees"
            out[bucket].append({"group": current_group, "name": name, "hop": hop})
        elif line.endswith(":") and not line.startswith(" "):
            current_group = line.rstrip(":")
    return out


async def _resolve_symbol(symbol: str, project: str) -> dict[str, Any] | None:
    """Resolve a symbol to {qualified_name, file, kind}; prefer source files."""
    text = _cb_text(await graph_mod.call("search_graph", {"name_pattern": symbol, "project": project}))
    rows = _parse_graph_rows(text, limit=12)
    if not rows:
        return None
    code_exts = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs", ".rb", ".php")
    code_rows = [r for r in rows if str(r.get("file", "")).lower().endswith(code_exts)]
    candidates = code_rows or rows
    exact = [r for r in candidates if r.get("name") == symbol]
    chosen = (exact or candidates)[0]
    return {
        "symbol": symbol,
        "qualified_name": chosen.get("qualified_name"),
        "file": chosen.get("file"),
        "kind": chosen.get("kind", ""),
    }


def _search_facts(query: str, user_id: str | None, top_k: int, project: str | None, threshold: float = 0.1) -> dict[str, Any]:
    """Semantic fact search with client-side project scoping and TTL flags."""
    fetch = top_k if not project else min(150, top_k * 3)
    raw = _mem0("search_memories", {"query": query, "user_id": user_id, "top_k": fetch, "threshold": threshold})
    rows = _mem0_rows(raw)
    if not project:
        rows, expired = _with_expiry(rows[:top_k])
        return {"results": rows, "scanned": len(rows), "dropped_other_project": 0, "unscoped_kept": 0, "expired": expired}
    kept: list[dict[str, Any]] = []
    dropped = unscoped = 0
    for row in rows:
        row_project = (row.get("metadata") or {}).get("project")
        if row_project == project:
            kept.append(row)
        elif row_project:
            dropped += 1
        else:
            unscoped += 1
            kept.append(row)
    kept, expired = _with_expiry(kept[:top_k])
    return {
        "results": kept,
        "scanned": len(rows),
        "dropped_other_project": dropped,
        "unscoped_kept": unscoped,
        "expired": expired,
    }


# â”€â”€ fact tools â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@mcp.tool(description="Store a durable fact in semantic memory. Use for verified findings, decisions and gotchas.")
async def add_memory(
    text: str,
    ctx: Context,
    user_id: str = "default_user",
    agent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    project: str | None = None,
    stale_after_days: int | None = None,
    stale_after: str | None = None,
    infer: bool = False,
    dedupe: bool = True,
) -> str:
    meta = dict(metadata or {})
    session_id = _session_id_from(ctx)
    if session_id and not meta.get("session"):
        meta["session"] = session_id
    if project and not meta.get("project"):
        meta["project"] = project
    if stale_after:
        meta["stale_after"] = stale_after
    elif stale_after_days and stale_after_days > 0 and not meta.get("stale_after"):
        meta["stale_after"] = (datetime.now(timezone.utc) + timedelta(days=stale_after_days)).isoformat()
    if dedupe:
        from .vitals import find_near_duplicate

        twin = find_near_duplicate(text, user_id)
        if twin:
            return _dump(
                {
                    "stored": None,
                    "duplicate_of": twin,
                    "text": text[:400],
                    "note": "A near-identical fact already exists; nothing was written. Pass dedupe=false to store anyway.",
                }
            )
    return _mem0(
        "add_memory",
        {"text": text, "user_id": user_id, "agent_id": agent_id, "metadata": meta, "infer": infer},
    )


@mcp.tool(description="Search semantic memory for facts matching a query (optionally scoped to a project).")
async def search_memories(
    query: str,
    ctx: Context,
    user_id: str | None = None,
    agent_id: str | None = None,
    top_k: int = 10,
    threshold: float = 0.1,
    project: str | None = None,
) -> str:
    payload = _search_facts(query, user_id, top_k, project, threshold)
    payload["project"] = project
    return _dump(payload)


@mcp.tool(description="List memories for a scope.")
async def get_memories(user_id: str | None = None, agent_id: str | None = None, top_k: int = 20) -> str:
    return _mem0("get_memories", {"user_id": user_id, "agent_id": agent_id, "top_k": top_k})


@mcp.tool(description="Fetch one memory by id.")
async def get_memory(memory_id: str) -> str:
    return _mem0("get_memory", {"memory_id": memory_id})


@mcp.tool(description="Update a memory's text/metadata.")
async def update_memory(memory_id: str, text: str | None = None, metadata: dict[str, Any] | None = None) -> str:
    return _mem0("update_memory", {"memory_id": memory_id, "text": text, "metadata": metadata})


@mcp.tool(description="Delete one memory by id.")
async def delete_memory(memory_id: str) -> str:
    return _mem0("delete_memory", {"memory_id": memory_id})


@mcp.tool(description="Delete all memories for a user or agent scope.")
async def delete_all_memories(user_id: str | None = None, agent_id: str | None = None) -> str:
    return _mem0("delete_all_memories", {"user_id": user_id, "agent_id": agent_id})


# â”€â”€ graph tools â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@mcp.tool(description="Find symbols in the code knowledge graph by name pattern.")
async def search_graph(
    name_pattern: str | None = None,
    project: str | None = None,
    query: str | None = None,
    label: str | None = None,
    qn_pattern: str | None = None,
    file_pattern: str | None = None,
    relationship: str | None = None,
    min_degree: int | None = None,
    max_degree: int | None = None,
    exclude_entry_points: bool | None = None,
    include_connected: bool | None = None,
) -> str:
    return await graph_mod.call(
        "search_graph",
        {
            "name_pattern": name_pattern,
            "project": project or await _default_project(),
            "query": query,
            "label": label,
            "qn_pattern": qn_pattern,
            "file_pattern": file_pattern,
            "relationship": relationship,
            "min_degree": min_degree,
            "max_degree": max_degree,
            "exclude_entry_points": exclude_entry_points,
            "include_connected": include_connected,
        },
    )


@mcp.tool(description="Trace callers/callees of a function in the code knowledge graph.")
async def trace_path(
    function_name: str,
    project: str | None = None,
    direction: str | None = None,
    depth: int | None = None,
    limit: int | None = None,
) -> str:
    return await graph_mod.call(
        "trace_path",
        {
            "function_name": function_name,
            "project": project or await _default_project(),
            "direction": direction,
            "depth": depth,
            "limit": limit,
        },
    )


@mcp.tool(description="Fetch the source snippet for a symbol (short name or qualified_name from search_graph).")
async def get_code_snippet(
    qualified_name: str,
    project: str | None = None,
    include_neighbors: bool | None = None,
) -> str:
    return await graph_mod.call(
        "get_code_snippet",
        {
            "qualified_name": qualified_name,
            "project": project or await _default_project(),
            "include_neighbors": include_neighbors,
        },
    )


@mcp.tool(description="Run a Cypher query against the code knowledge graph.")
async def query_graph(
    query: str,
    project: str | None = None,
    graph: str | None = None,
    max_rows: int | None = None,
) -> str:
    return await graph_mod.call(
        "query_graph",
        {"query": query, "project": project or await _default_project(), "graph": graph, "max_rows": max_rows},
    )


@mcp.tool(description="Architecture summary of an indexed project.")
async def get_architecture(
    project: str | None = None,
    path: str | None = None,
    aspects: list[str] | None = None,
) -> str:
    return await graph_mod.call(
        "get_architecture",
        {"project": project or await _default_project(), "path": path, "aspects": aspects},
    )


@mcp.tool(description="Verify index coverage for specific paths or scopes in an indexed project.")
async def check_index_coverage(
    project: str | None = None,
    paths: list[str] | None = None,
    scopes: list[str] | None = None,
    scope_limit: int | None = None,
    scope_offset: int | None = None,
) -> str:
    return await graph_mod.call(
        "check_index_coverage",
        {
            "project": project or await _default_project(),
            "paths": paths,
            "scopes": scopes,
            "scope_limit": scope_limit,
            "scope_offset": scope_offset,
        },
    )


@mcp.tool(description="Map the blast radius of a change in an indexed project.")
async def detect_changes(
    project: str | None = None,
    scope: str | None = None,
    direction: str | None = None,
    depth: int | None = None,
    limit: int | None = None,
    base_branch: str | None = None,
    since: str | None = None,
    format: str | None = None,
) -> str:
    return await graph_mod.call(
        "detect_changes",
        {
            "project": project or await _default_project(),
            "scope": scope,
            "direction": direction,
            "depth": depth,
            "limit": limit,
            "base_branch": base_branch,
            "since": since,
            "format": format,
        },
    )


@mcp.tool(description="List projects indexed by the code knowledge graph.")
async def list_projects() -> str:
    return await graph_mod.call("list_projects", {})


@mcp.tool(description="Index status for a project (node/edge counts, freshness).")
async def index_status(project: str | None = None, verbose: bool | None = None) -> str:
    args: dict[str, Any] = {"verbose": verbose}
    if project:
        args["project"] = project
    return await graph_mod.call("index_status", args)


@mcp.tool(description="Index (or re-index) a repository into the code knowledge graph.")
async def index_repository(
    repo_path: str,
    mode: str | None = None,
    name: str | None = None,
    target_projects: list[str] | None = None,
    persistence: bool | None = None,
) -> str:
    return await graph_mod.call(
        "index_repository",
        {
            "repo_path": repo_path,
            "mode": mode,
            "name": name,
            "target_projects": target_projects,
            "persistence": persistence,
        },
    )


@mcp.tool(description="Remove a project from the code knowledge graph index.")
async def delete_project(project: str) -> str:
    return await graph_mod.call("delete_project", {"project": project})


@mcp.tool(description="Generic escape hatch for any graph tool (tool=name, arguments=JSON object).")
async def codebase_memory(tool: str, arguments: str = "{}") -> str:
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        return _dump({"error": f"arguments is not valid JSON: {exc}"})
    return await graph_mod.call(tool, args if isinstance(args, dict) else {})


# â”€â”€ cross-linked tools â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@mcp.tool(
    description=(
        "Store a verified fact AND link it to code: resolves each symbol name against the "
        "code knowledge graph and stores the qualified name + file in the fact metadata, so "
        "the fact can later be recalled from the symbol."
    )
)
async def remember_fact(
    text: str,
    ctx: Context,
    symbols: list[str] | None = None,
    project: str | None = None,
    user_id: str = "default_user",
    session_id: str | None = None,
    agent_id: str | None = None,
    stale_after_days: int | None = None,
    stale_after: str | None = None,
    dedupe: bool = True,
) -> str:
    project = project or await _default_project()
    links: list[dict[str, Any]] = []
    for symbol in symbols or []:
        resolved = await _resolve_symbol(symbol, project)
        links.append(resolved or {"symbol": symbol, "qualified_name": None, "file": None, "kind": ""})
    if dedupe:
        from .vitals import find_near_duplicate

        twin = find_near_duplicate(text, user_id)
        if twin:
            return _dump(
                {
                    "stored": twin["id"],
                    "duplicate_of": twin,
                    "text": text[:400],
                    "linked_symbols": links,
                    "project": project,
                    "note": "A near-identical fact already exists; the existing id is returned and nothing was written. Pass dedupe=false to store anyway.",
                }
            )
    metadata: dict[str, Any] = {"project": project}
    if links:
        metadata["symbols"] = links
    resolved_session = session_id or _session_id_from(ctx)
    if resolved_session:
        metadata["session"] = resolved_session
    if stale_after:
        metadata["stale_after"] = stale_after
    elif stale_after_days and stale_after_days > 0:
        metadata["stale_after"] = (datetime.now(timezone.utc) + timedelta(days=stale_after_days)).isoformat()
    raw = _mem0(
        "add_memory",
        {"text": text, "user_id": user_id, "agent_id": agent_id, "metadata": metadata, "infer": False},
    )
    rows = _mem0_rows(raw)
    return _dump(
        {
            "stored": rows[0].get("id") if rows else None,
            "text": text[:400],
            "linked_symbols": links,
            "project": project,
        }
    )


@mcp.tool(
    description=(
        "One fused recall across BOTH memory surfaces: semantic facts AND matching code symbols, "
        "with the facts linked to those symbols called out. Use this instead of separate "
        "search_memories + search_graph."
    )
)
async def recall(
    query: str,
    ctx: Context,
    project: str | None = None,
    user_id: str = "amadeus",
    top_k: int = 5,
    include_code: bool = True,
) -> str:
    project = project or await _default_project()
    found = _search_facts(query, user_id, top_k, project)
    facts = found["results"]
    code_rows: list[dict[str, Any]] = []
    if include_code:
        text = _cb_text(await graph_mod.call("search_graph", {"query": query, "project": project}))
        code_rows = _parse_graph_rows(text)
    hit_qns = {row["qualified_name"] for row in code_rows}
    hit_names = {row["name"] for row in code_rows}
    linked_facts = []
    for fact in facts:
        meta = fact.get("metadata") or {}
        for link in meta.get("symbols", []) or []:
            qn = link.get("qualified_name") or ""
            if (qn and qn in hit_qns) or link.get("symbol") in hit_names:
                linked_facts.append({"id": fact.get("id"), "memory": fact.get("memory"), "via": link.get("symbol")})
                break
    return _dump(
        {
            "query": query,
            "project": project,
            "facts": [
                {
                    "id": f.get("id"),
                    "memory": (f.get("memory") or "")[:400],
                    "score": f.get("score"),
                    "symbols": (f.get("metadata") or {}).get("symbols"),
                    "ttl": f.get("ttl"),
                }
                for f in facts
            ],
            "code": code_rows,
            "facts_linked_to_code": linked_facts,
            "dropped_other_project": found.get("dropped_other_project", 0),
            "unscoped_kept": found.get("unscoped_kept", 0),
            "expired": found.get("expired", 0),
        }
    )


@mcp.tool(
    description=(
        "Reverse index: every stored fact linked to a given symbol (or file). Answers "
        "'what do we know about this function?' â€” the opposite direction of remember_fact."
    )
)
async def facts_for_symbol(
    symbol: str,
    user_id: str = "amadeus",
    limit: int = 25,
    project: str | None = None,
) -> str:
    rows = _mem0_rows(_mem0("get_memories", {"user_id": user_id, "top_k": 500}))
    needle = symbol.strip().lower()
    hits: list[dict[str, Any]] = []
    for row in rows:
        meta = row.get("metadata") or {}
        if project and meta.get("project") and meta.get("project") != project:
            continue
        for link in meta.get("symbols", []) or []:
            candidates = [
                str(link.get("symbol") or ""),
                str(link.get("qualified_name") or ""),
                str(link.get("file") or ""),
            ]
            basename = str(link.get("file") or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if basename.endswith(".py"):
                basename = basename[:-3]
            match = any(needle and (needle == c.lower() or needle in c.lower()) for c in candidates) or (
                needle and needle == basename.lower()
            )
            if match:
                hits.append(
                    {
                        "id": row.get("id"),
                        "memory": (row.get("memory") or "")[:400],
                        "created_at": row.get("created_at"),
                        "via": link.get("symbol"),
                        "file": link.get("file"),
                        "project": meta.get("project"),
                        "session": meta.get("session"),
                    }
                )
                break
    return _dump({"symbol": symbol, "scanned": len(rows), "matched": len(hits), "facts": hits[:limit]})


@mcp.tool(
    description=(
        "Check whether a stored fact is still fresh: for every symbol linked to the fact, checks "
        "whether it still exists in the code knowledge graph (and whether its file moved), and "
        "whether the fact's TTL has passed."
    )
)
async def fact_freshness(memory_id: str, project: str | None = None) -> str:
    rows = _mem0_rows(_mem0("get_memory", {"memory_id": memory_id}))
    if not rows:
        return _dump({"error": f"fact {memory_id} not found"})
    fact = rows[0]
    meta = fact.get("metadata") or {}
    project = project or meta.get("project") or _default_project()
    checks = []
    for link in meta.get("symbols", []) or []:
        symbol = link.get("symbol") or ""
        if not symbol:
            continue
        resolved = await _resolve_symbol(symbol, project)
        checks.append(
            {
                "symbol": symbol,
                "still_exists": bool(resolved),
                "qualified_name": (resolved or {}).get("qualified_name"),
                "file": (resolved or {}).get("file"),
                "file_moved": bool(resolved and link.get("file") and resolved.get("file") != link.get("file")),
            }
        )
    ttl = _expiry_of(fact)
    symbol_stale = any(c["file_moved"] or not c["still_exists"] for c in checks)
    verdict = "stale" if (symbol_stale or ttl["expired"]) else ("fresh" if checks else "no-links")
    return _dump(
        {
            "memory_id": memory_id,
            "memory": (fact.get("memory") or "")[:300],
            "links": checks,
            "ttl": ttl,
            "symbol_stale": symbol_stale,
            "verdict": verdict,
        }
    )


@mcp.tool(
    description=(
        "Blast radius of a stored fact: for every symbol linked to the fact, who calls it and "
        "what it calls, plus â€” optionally â€” the impact of the current working-tree changes."
    )
)
async def fact_impact(
    memory_id: str,
    project: str | None = None,
    direction: str = "inbound",
    depth: int = 2,
    limit: int = 50,
    include_changes: bool = False,
) -> str:
    rows = _mem0_rows(_mem0("get_memory", {"memory_id": memory_id}))
    if not rows:
        return _dump({"error": f"fact {memory_id} not found"})
    fact = rows[0]
    meta = fact.get("metadata") or {}
    project = project or meta.get("project") or _default_project()
    symbols: list[dict[str, Any]] = []
    for link in meta.get("symbols", []) or []:
        name = link.get("symbol") or ""
        if not name:
            continue
        trace = _parse_trace(
            _cb_text(
                await graph_mod.call(
                    "trace_path",
                    {"function_name": name, "project": project, "direction": direction, "depth": depth, "limit": limit},
                )
            )
        )
        symbols.append(
            {
                "symbol": name,
                "qualified_name": link.get("qualified_name"),
                "file": link.get("file"),
                "kind": link.get("kind"),
                "direction": direction,
                "callers_total": trace.get("callers_total"),
                "callers": trace.get("callers", [])[:limit],
                "callees_total": trace.get("callees_total"),
                "callees": trace.get("callees", [])[:limit],
            }
        )
    payload: dict[str, Any] = {
        "memory_id": memory_id,
        "memory": (fact.get("memory") or "")[:300],
        "symbols": symbols,
    }
    if include_changes:
        changes = _cb_text(
            await graph_mod.call(
                "detect_changes",
                {"project": project, "scope": "impact", "direction": direction, "depth": depth, "limit": limit},
            )
        )
        head = [line for line in changes.splitlines() if not line.startswith(" ")][:8]
        payload["current_changes"] = {
            "header": head,
            "changed_files_total": next(
                (int(line.split(":")[1]) for line in changes.splitlines() if line.startswith("changed_files:")),
                None,
            ),
        }
    return _dump(payload)


@mcp.tool(description="List stored facts for one project (or the unscoped ones).")
async def facts_by_project(
    project: str | None = None,
    user_id: str = "amadeus",
    limit: int = 50,
) -> str:
    rows = _mem0_rows(_mem0("get_memories", {"user_id": user_id, "top_k": 500}))
    kept: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        meta = row.get("metadata") or {}
        row_project = meta.get("project")
        key = str(row_project or "unscoped")
        counts[key] = counts.get(key, 0) + 1
        if project is None:
            if row_project:
                continue
        elif row_project != project:
            continue
        kept.append(
            {
                "id": row.get("id"),
                "memory": (row.get("memory") or "")[:300],
                "project": row_project,
                "session": meta.get("session"),
                "symbols": [link.get("symbol") for link in (meta.get("symbols") or [])],
            }
        )
    return _dump(
        {
            "project": project or "unscoped",
            "scanned": len(rows),
            "matched": len(kept),
            "facts_by_project": counts,
            "facts": kept[:limit],
        }
    )


EXPIRING_SOON_DAYS = 7


@mcp.tool(
    description=(
        "List facts whose TTL has expired (metadata.stale_after in the past), plus those expiring "
        "within 7 days. Expired facts are still returned by searches but flagged."
    )
)
async def expired_facts(user_id: str = "amadeus", limit: int = 50) -> str:
    rows = _mem0_rows(_mem0("get_memories", {"user_id": user_id, "top_k": 500}))
    expired: list[dict[str, Any]] = []
    soon: list[dict[str, Any]] = []
    for row in rows:
        state = _expiry_of(row)
        if state["expired"]:
            expired.append(
                {
                    "id": row.get("id"),
                    "memory": (row.get("memory") or "")[:300],
                    "expired_at": state["expires_at"],
                    "project": (row.get("metadata") or {}).get("project"),
                    "symbols": [link.get("symbol") for link in ((row.get("metadata") or {}).get("symbols") or [])],
                }
            )
        elif state["days_left"] is not None and state["days_left"] <= EXPIRING_SOON_DAYS:
            soon.append({"id": row.get("id"), "memory": (row.get("memory") or "")[:200], "days_left": state["days_left"]})
    return _dump(
        {
            "scanned": len(rows),
            "expired_count": len(expired),
            "expiring_within_7d": len(soon),
            "expired": expired[:limit],
            "expiring_soon": soon[:limit],
        }
    )


@mcp.custom_route("/health", methods=["GET"])
async def rest_health(request: Any) -> Any:
    """Plain HTTP health of the memory server (for the brain and the watchdog)."""
    from starlette.responses import JSONResponse

    from .graph import codebase_memory_exe
    from .store import backend, store_dir

    return JSONResponse(
        {
            "status": "ok",
            "service": "unified-memory-mcp",
            "store_dir": str(store_dir()),
            "backend": backend(),
            "graph_binary": codebase_memory_exe(),
        }
    )


@mcp.custom_route("/facts", methods=["POST"])
async def rest_add_fact(request: Any) -> Any:
    """Append a fact over plain HTTP.

    The brain writes here instead of speaking MCP: it has no usable MCP SDK in
    its own virtualenv (pydantic 2.9.2 vs the SDK's 2.11 requirement), and a
    dependency-free REST call cannot break the brain's event loop.
    """
    from starlette.responses import JSONResponse

    try:
        payload = await request.json()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"invalid JSON body: {exc}"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"}, status_code=400)

    text = str(payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text is required"}, status_code=400)
    symbols = payload.get("symbols") or []
    metadata: dict[str, Any] = dict(payload.get("metadata") or {})
    if payload.get("project"):
        metadata.setdefault("project", payload["project"])
    if payload.get("source"):
        metadata.setdefault("source", payload["source"])
    if payload.get("session"):
        metadata.setdefault("session", payload["session"])
    if payload.get("stale_after_days"):
        metadata.setdefault(
            "stale_after",
            (datetime.now(timezone.utc) + timedelta(days=int(payload["stale_after_days"]))).isoformat(),
        )
    if symbols and "symbols" not in metadata:
        metadata["symbols"] = symbols
    dedupe = bool(payload.get("dedupe", True))
    if dedupe:
        from .vitals import find_near_duplicate

        twin = find_near_duplicate(text, str(payload.get("user_id") or "amadeus"))
        if twin:
            # Idempotent write: satisfied, no new row, the caller gets the
            # existing fact's id instead of a fresh one.
            return JSONResponse(
                {"ok": True, "duplicate_of": twin, "id": twin["id"], "text": text[:200]},
                status_code=200,
            )
    raw = store_mod.call(
        "add_memory",
        {
            "text": text,
            "user_id": payload.get("user_id") or "amadeus",
            "metadata": metadata or None,
            "infer": False,
        },
    )
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {}
    rows = parsed.get("results") if isinstance(parsed, dict) else None
    return JSONResponse({"ok": bool(rows), "id": (rows or [{}])[0].get("id"), "text": text[:200]})


@mcp.custom_route("/facts", methods=["GET"])
async def rest_list_facts(request: Any) -> Any:
    """List stored facts over plain HTTP (read-only)."""
    from starlette.responses import JSONResponse

    user_id = request.query_params.get("user_id", "amadeus")
    limit = int(request.query_params.get("limit", "50"))
    raw = store_mod.call("get_memories", {"user_id": user_id, "top_k": limit})
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {}
    rows = parsed.get("results", []) if isinstance(parsed, dict) else []
    return JSONResponse(
        {
            "user_id": user_id,
            "count": len(rows),
            "facts": [
                {
                    "id": row.get("id"),
                    "memory": (row.get("memory") or "")[:300],
                    "metadata": row.get("metadata"),
                }
                for row in rows[:limit]
            ],
        }
    )


def main() -> int:
    """Console entry point."""
    parser = argparse.ArgumentParser(prog="unified-memory-mcp", description=__doc__)
    parser.add_argument("--stdio", action="store_true", help="serve over stdio instead of Streamable HTTP")
    parser.add_argument("--host", default=os.environ.get("MEM0_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MEM0_PORT", "8777")))
    args = parser.parse_args()

    if args.stdio:
        mcp.run(transport="stdio")
        return 0

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    print(f"unified-memory-mcp listening on http://{args.host}:{args.port}/mcp", file=sys.stderr)
    print(f"  fact store : {store_mod.store_dir()} (backend: {store_mod.backend()})", file=sys.stderr)
    exe = graph_mod.codebase_memory_exe()
    print(f"  code graph : {exe or 'not installed â€” graph tools will report an error'}", file=sys.stderr)
    asyncio.run(graph_mod.prewarm())
    mcp.run(transport="streamable-http")
    return 0


@mcp.tool(
    description=(
        "One-call health picture of the local AMADEUS system: brain status, scheduler jobs, "
        "open circuit breakers, dead-letter queue, vault reachability, response-cache hit rate "
        "and the brain's own uptime. Reports 'unreachable' instead of failing when the brain "
        "is not running."
    )
)
async def system_vitals() -> str:
    from .vitals import vitals as _vitals

    return _dump(_vitals())


@mcp.tool(
    description=(
        "Audit the fact store: how many facts are scoped per project, how many are unscoped, "
        "expired, missing provenance or symbol links, and which pairs are near-duplicates. "
        "Use it before trusting recall quality."
    )
)
async def memory_audit(user_id: str = "amadeus", duplicate_threshold: float = 0.82) -> str:
    from .vitals import audit as _audit

    return _dump(_audit(user_id=user_id, duplicate_threshold=duplicate_threshold))


@mcp.tool(
    description=(
        "Assign a project scope to facts that have none (metadata only — fact text is never "
        "rewritten). Without an explicit project the scope is derived from the fact's linked "
        "symbols. Defaults to a dry run: set dry_run=false to apply."
    )
)
async def memory_scope_facts(
    user_id: str = "amadeus",
    project: str | None = None,
    dry_run: bool = True,
    limit: int = 200,
) -> str:
    from .vitals import scope_facts as _scope

    return _dump(_scope(user_id=user_id, project=project, dry_run=dry_run, limit=limit))


@mcp.tool(
    description=(
        "Remove near-duplicate facts, keeping the OLDEST copy of each pair (only pairs above the "
        "similarity threshold are touched). Defaults to a dry run: set dry_run=false to delete."
    )
)
async def memory_dedupe(
    user_id: str = "amadeus",
    threshold: float = 0.95,
    dry_run: bool = True,
    limit: int = 50,
) -> str:
    from .vitals import dedupe as _dedupe

    return _dump(_dedupe(user_id=user_id, threshold=threshold, dry_run=dry_run, limit=limit))


@mcp.tool(
    description=(
        "Delete facts whose TTL has passed. Defaults to a dry run: set dry_run=false to delete."
    )
)
async def memory_purge_expired(user_id: str = "amadeus", dry_run: bool = True, limit: int = 200) -> str:
    from .vitals import purge_expired as _purge

    return _dump(_purge(user_id=user_id, dry_run=dry_run, limit=limit))


if __name__ == "__main__":
    raise SystemExit(main())
