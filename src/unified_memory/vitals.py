"""System vitals + memory hygiene tools for the unified memory MCP.

Two surfaces that the MCP was missing:

``system_vitals``
    One call that answers "is the whole system OK?" — the brain's health and
    deep health, the scheduler, open circuit breakers, the Telegram poller,
    the LLM tier that served recent traffic, today's cost, and the freshness
    of the code index. It reads the brain's own HTTP API when it is running
    and degrades to "unknown" instead of failing when it is not.

``memory_audit`` / ``memory_scope_facts`` / ``memory_purge_expired``
    The fact store accumulates untidy rows: facts with no project scope,
    near-duplicates from repeated writes, expired facts that nobody removes.
    The audit reports the shape of the store; the two mutators are explicit
    and conservative (metadata-only scoping, deletion behind a dry-run flag).
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from . import store as store_mod

BRAIN_URL = os.environ.get("AMADEUS_BRAIN_URL", "http://127.0.0.1:8000")
HTTP_TIMEOUT = float(os.environ.get("AMADEUS_VITALS_TIMEOUT", "8"))
HTTP_ERROR_STATUS = 400
MIN_TOKEN_LENGTH = 2
MAX_REPORTED_DUPLICATES = 25
MIN_SIMILARITY = 0.82


def _get_json(path: str) -> Any:
    """GET a JSON endpoint from the brain; None when unreachable."""
    try:
        import httpx
    except ImportError:  # pragma: no cover
        return None
    try:
        response = httpx.get(f"{BRAIN_URL}{path}", timeout=HTTP_TIMEOUT)
        if response.status_code >= HTTP_ERROR_STATUS:
            return {"error": f"HTTP {response.status_code}"}
        return response.json()
    except Exception as exc:
        return {"error": f"unreachable ({type(exc).__name__})"}


def vitals() -> dict[str, Any]:
    """Collect a compact health picture of the local AMADEUS system."""
    health = _get_json("/health")
    deep = _get_json("/health/deep")
    report: dict[str, Any] = {
        "brain_url": BRAIN_URL,
        "brain": "unreachable",
        "brain_detail": health,
        "scheduler": None,
        "breakers": None,
        "vault": None,
        "cache": None,
    }
    if isinstance(health, dict) and health.get("status"):
        report["brain"] = health.get("status")
    if isinstance(deep, dict) and isinstance(deep.get("checks"), dict):
        checks = deep["checks"]
        scheduler = checks.get("scheduler") or {}
        breakers = checks.get("circuit_breakers") or {}
        vault = checks.get("vault_manager") or {}
        cache = checks.get("response_cache") or {}
        report["scheduler"] = {
            "running": scheduler.get("running"),
            "jobs": scheduler.get("jobs_count"),
            "status": scheduler.get("status"),
        }
        report["breakers"] = {
            "open": breakers.get("open_breakers"),
            "status": breakers.get("status"),
        }
        report["vault"] = {"status": vault.get("status"), "path": vault.get("path")}
        report["cache"] = {
            "status": cache.get("status"),
            "hit_rate": cache.get("hit_rate"),
            "rows": cache.get("rows"),
        }
        report["dead_letter_queue"] = (checks.get("consciousness_bus") or {}).get("dead_letter_queue_size")
    return report


# ── memory hygiene ─────────────────────────────────────────────────────────
_STOPWORDS = frozenset(
    [
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "and", "or", "but", "if", "then",
        "than", "that", "this", "these", "those", "of", "to", "in", "on", "for", "with", "as", "by", "at",
        "it", "its", "from", "not", "no", "do", "does", "did", "done", "have", "has", "had", "i", "we",
        "you", "they", "he", "she", "them", "us", "my", "our", "your", "their", "can", "could", "will",
        "would", "should", "may", "might", "must", "there", "here", "what", "which", "who", "whom", "how",
        "when", "where", "why",
    ]
)


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > MIN_TOKEN_LENGTH}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def audit(user_id: str = "amadeus", duplicate_threshold: float = 0.82) -> dict[str, Any]:
    """Audit the fact store: scope coverage, duplicates, expiry, provenance."""
    rows = store_mod.mem0_rows(store_mod.call("get_memories", {"user_id": user_id, "top_k": 500}))
    from .store import expiry_of  # shared helper

    scoped: dict[str, int] = {}
    unscoped = 0
    expired = 0
    no_provenance = 0
    no_symbols = 0
    tokenized: list[tuple[str, set[str]]] = []
    for row in rows:
        meta = row.get("metadata") or {}
        project = meta.get("project")
        if project:
            scoped[project] = scoped.get(project, 0) + 1
        else:
            unscoped += 1
        state = expiry_of(row)
        if state["expired"]:
            expired += 1
        if not meta.get("session") and not project:
            no_provenance += 1
        if not meta.get("symbols"):
            no_symbols += 1
        tokenized.append((row.get("id") or "", _tokens(row.get("memory") or "")))

    duplicates: list[dict[str, Any]] = []
    for index, (id_a, tokens_a) in enumerate(tokenized):
        for id_b, tokens_b in tokenized[index + 1 :]:
            if id_a == id_b:
                continue
            score = _jaccard(tokens_a, tokens_b)
            if score >= duplicate_threshold:
                duplicates.append({"a": id_a, "b": id_b, "similarity": round(score, 3)})
                if len(duplicates) >= MAX_REPORTED_DUPLICATES:
                    break
        if len(duplicates) >= MAX_REPORTED_DUPLICATES:
            break

    total = len(rows)
    suggestions: list[str] = []
    if unscoped:
        suggestions.append(
            f"{unscoped} facts have no project scope, so they surface in every project's recall. "
            "That is correct for general knowledge — scope the project-specific ones with "
            "memory_scope_facts."
        )
    if duplicates:
        suggestions.append(
            f"{len(duplicates)} near-duplicate pairs (the same fact stored more than once) — "
            "memory_dedupe removes the newer copy."
        )
    if expired:
        suggestions.append(f"{expired} facts are past their TTL — memory_purge_expired removes them.")
    if no_symbols:
        suggestions.append(f"{no_symbols} facts are not linked to any code symbol, so they cannot be re-validated.")

    return {
        "user_id": user_id,
        "total": total,
        "by_project": scoped,
        "unscoped": unscoped,
        "expired": expired,
        "without_provenance": no_provenance,
        "without_symbol_links": no_symbols,
        "near_duplicates": duplicates,
        "suggestions": suggestions,
    }


def scope_facts(
    user_id: str = "amadeus",
    project: str | None = None,
    dry_run: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """Assign a project scope to facts that have none.

    Metadata-only and non-destructive: a fact's text is never rewritten.
    ``project=None`` derives the scope from the fact's linked symbols (the
    graph knows which repository a file belongs to); otherwise the given
    project is applied.
    """
    rows = store_mod.mem0_rows(store_mod.call("get_memories", {"user_id": user_id, "top_k": 500}))
    targets: list[dict[str, Any]] = []
    for row in rows:
        meta = row.get("metadata") or {}
        if meta.get("project"):
            continue
        if project:
            assigned = project
            basis = "explicit"
        else:
            assigned = None
            basis = "unresolved"
            for link in meta.get("symbols", []) or []:
                assigned = _project_from_link(link)
                if assigned:
                    basis = "symbol-prefix"
                    break
        if assigned:
            targets.append({"id": row.get("id"), "project": assigned, "basis": basis, "memory": (row.get("memory") or "")[:120]})

    if dry_run:
        return {
            "dry_run": True,
            "would_scope": len(targets),
            "targets": targets[:limit],
        }

    updated = 0
    for target in targets:
        raw = store_mod.call("get_memory", {"memory_id": target["id"]})
        try:
            fact = json.loads(raw)
        except Exception as exc:
            print(f"[unified-memory] skipping unparsable fact {target['id']}: {exc!r}", file=sys.stderr)
            continue
        meta = (fact.get("metadata") or {}) if isinstance(fact, dict) else {}
        meta["project"] = target["project"]
        result = store_mod.call_tool("update_memory", {"memory_id": target["id"], "metadata": meta})
        if result:
            updated += 1
    return {"dry_run": False, "scoped": updated, "targets": targets[:limit]}


def _project_from_link(link: dict[str, Any]) -> str | None:
    """Derive the project from a linked symbol.

    The graph's qualified names are prefixed with the project
    (``C-AMADEUS.brain.cognition...``), so the prefix IS the project name —
    no extra lookup needed.
    """
    qualified = str(link.get("qualified_name") or "")
    if not qualified or "." not in qualified:
        return None
    candidate = qualified.split(".", 1)[0]
    if not candidate or " " in candidate:
        return None
    return candidate


def dedupe(user_id: str = "amadeus", threshold: float = 0.95, dry_run: bool = True, limit: int = 50) -> dict[str, Any]:
    """Remove near-duplicate facts, keeping the OLDEST copy of each pair.

    Only pairs at or above ``threshold`` (default 0.95 — effectively the same
    text) are touched, and the NEWER row is the one deleted, so the surviving
    fact keeps the original timestamp. Dry run by default.
    """
    rows = store_mod.mem0_rows(store_mod.call("get_memories", {"user_id": user_id, "top_k": 500}))
    kept: list[tuple[str, float, set[str]]] = []
    victims: list[dict[str, Any]] = []
    for row in rows:
        fact_id = row.get("id") or ""
        created = row.get("created_at") or 0
        tokens = _tokens(row.get("memory") or "")
        if not tokens:
            continue
        duplicate = next(
            ((kept_id, kept_created, index) for index, (kept_id, kept_created, kept_tokens) in enumerate(kept) if _jaccard(tokens, kept_tokens) >= threshold),
            None,
        )
        if duplicate is None:
            kept.append((fact_id, created, tokens))
            continue
        kept_id, kept_created, index = duplicate
        if created >= kept_created:
            victims.append({"delete": fact_id, "keep": kept_id})
        else:
            # The newcomer is the older copy: keep it, drop the one we had.
            victims.append({"delete": kept_id, "keep": fact_id})
            kept[index] = (fact_id, created, tokens)
    if dry_run:
        return {"dry_run": True, "would_delete": len(victims), "pairs": victims[:limit]}
    deleted = 0
    for victim in victims[:limit]:
        if store_mod.call_tool("delete_memory", {"memory_id": victim["delete"]}):
            deleted += 1
    return {"dry_run": False, "deleted": deleted, "pairs": victims[:limit]}


def purge_expired(user_id: str = "amadeus", dry_run: bool = True, limit: int = 200) -> dict[str, Any]:
    """Delete facts whose TTL has passed."""
    from .store import expiry_of

    rows = store_mod.mem0_rows(store_mod.call("get_memories", {"user_id": user_id, "top_k": 500}))
    expired = [
        {
            "id": row.get("id"),
            "memory": (row.get("memory") or "")[:160],
            "expired_at": expiry_of(row)["expires_at"],
            "project": (row.get("metadata") or {}).get("project"),
        }
        for row in rows
        if expiry_of(row)["expired"]
    ]
    if dry_run:
        return {"dry_run": True, "would_delete": len(expired), "facts": expired[:limit]}
    deleted = 0
    for item in expired[:limit]:
        if store_mod.call_tool("delete_memory", {"memory_id": item["id"]}):
            deleted += 1
    return {"dry_run": False, "deleted": deleted, "facts": expired[:limit]}
