"""Write-side dedup: repeated writes of the same fact must not pile up rows.

Covers the three write paths the store has (MCP ``add_memory``, MCP
``remember_fact``, REST ``POST /facts``) plus the ``find_near_duplicate``
helper itself. The store is faked in-process, so nothing here touches a
real memory store and no codebase-memory binary is needed.

Run:  python tests/test_write_dedupe.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Use a throwaway store path even though the store is faked below, so the
# package env guards never see a production directory.
os.environ["MEM0_DIR"] = tempfile.mkdtemp(prefix="unified-memory-dedupe-")

from unified_memory import server as srv  # noqa: E402
from unified_memory import store as store_mod  # noqa: E402
from unified_memory import vitals as vitals_mod  # noqa: E402

FACT_TEXT = "VERIFIED 2026-11-14: metrics HELP text falls back to the metric name"


class FakeStore:
    """Minimal mem0 stand-in: rows in memory, JSON in / JSON out."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.add_calls = 0

    def seed(self, text: str, **meta) -> dict:
        row = {
            "id": uuid.uuid4().hex,
            "memory": text,
            "score": 1.0,
            "user_id": meta.get("user_id", "amadeus"),
            "metadata": meta.get("metadata", {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.rows.append(row)
        return row

    def call(self, tool: str, args: dict) -> str:
        if tool == "get_memories":
            user = args.get("user_id")
            return json.dumps({"results": [r for r in self.rows if r.get("user_id") == user]})
        if tool == "add_memory":
            return json.dumps({"results": [self._add(args)]})
        return json.dumps({"results": []})

    def call_tool(self, name: str, args: dict):
        if name == "add_memory":
            return {"results": [self._add(args)]}
        if name == "get_memories":
            user = args.get("user_id")
            return {"results": [r for r in self.rows if r.get("user_id") == user]}
        return {"results": []}

    def _add(self, args: dict) -> dict:
        self.add_calls += 1
        row = {
            "id": uuid.uuid4().hex,
            "memory": args.get("text", ""),
            "score": 1.0,
            "user_id": args.get("user_id", "default_user"),
            "metadata": args.get("metadata") or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.rows.append(row)
        return row


class FakeRequest:
    """Starlette request stand-in for the REST handler."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class FakeCtx:
    """Context whose request_context.meta read fails, like a bare MCP call."""

    @property
    def request_context(self):
        raise AttributeError("no context in tests")


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "ok " if ok else "FAIL"
    print(f"  [{mark}] {label}{' — ' + detail if detail else ''}")
    if not ok:
        raise SystemExit(f"dedupe check failed: {label}")


def main() -> None:
    http_ok = 200
    store = FakeStore()
    store_mod.call = store.call  # type: ignore[method-assign]
    store_mod.call_tool = store.call_tool  # type: ignore[method-assign]
    srv.store_mod.call = store.call  # type: ignore[attr-defined]
    srv.store_mod.call_tool = store.call_tool  # type: ignore[attr-defined]

    existing = store.seed(FACT_TEXT, user_id="amadeus")

    # 1. helper: exact re-statement is caught
    hit = vitals_mod.find_near_duplicate(FACT_TEXT, "amadeus")
    check("find_near_duplicate catches an exact duplicate", hit is not None and hit["id"] == existing["id"])

    # 2. helper: unrelated text does not block a write
    miss = vitals_mod.find_near_duplicate("Completely different topic: kubernetes ingress TLS", "amadeus")
    check("unrelated text returns None", miss is None)

    # 3. helper: expired twins must not block a fresh restatement
    #    (fresh scope holding ONLY an expired twin — an unexpired copy
    #    would legitimately match and the hit would be correct)
    store.rows.clear()
    store.seed(
        FACT_TEXT,
        user_id="amadeus",
        metadata={"stale_after": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
    )
    after_expiry = vitals_mod.find_near_duplicate(FACT_TEXT, "amadeus")
    check(
        "expired duplicate is skipped",
        after_expiry is None,
        f"(still matched id={after_expiry['id'] if after_expiry else None})",
    )

    # restore the live twin the later checks write against
    store.rows.clear()
    existing = store.seed(FACT_TEXT, user_id="amadeus")

    # 4. MCP add_memory: duplicate write is refused, store did not grow
    store_mod.call = store.call  # type: ignore[method-assign]
    srv.store_mod.call = store.call  # type: ignore[attr-defined]
    srv.store_mod.call_tool = store.call_tool  # type: ignore[attr-defined]
    before = len(store.rows)
    out = json.loads(asyncio.run(srv.add_memory(FACT_TEXT, FakeCtx(), user_id="amadeus")))
    check(
        "add_memory returns duplicate_of and writes nothing",
        out.get("stored") is None and out.get("duplicate_of", {}).get("id") == existing["id"] and len(store.rows) == before,
    )

    # 5. dedupe=false override still stores
    before = len(store.rows)
    out = json.loads(
        asyncio.run(srv.add_memory(FACT_TEXT, FakeCtx(), user_id="amadeus", dedupe=False))
    )
    stored_id = (out.get("results") or [{}])[0].get("id")
    check("dedupe=false stores anyway", stored_id is not None and len(store.rows) == before + 1)

    # 6. remember_fact returns the existing id instead of a new row
    before = len(store.rows)
    out = json.loads(asyncio.run(srv.remember_fact(FACT_TEXT, FakeCtx(), user_id="amadeus")))
    similarity = (out.get("duplicate_of") or {}).get("similarity", 0)
    check(
        "remember_fact is idempotent on duplicates",
        out.get("stored") == existing["id"] and similarity >= vitals_mod.MIN_SIMILARITY and len(store.rows) == before,
    )

    # 7. REST POST /facts: ok=true, no new row, existing id returned
    before = len(store.rows)
    resp = asyncio.run(srv.rest_add_fact(FakeRequest({"text": FACT_TEXT, "user_id": "amadeus"})))
    body = json.loads(resp.body)
    check(
        "POST /facts duplicate is idempotent",
        resp.status_code == http_ok and body.get("ok") is True and body.get("id") == existing["id"] and len(store.rows) == before,
    )

    # 8. REST POST /facts: unrelated fact still stores normally
    resp = asyncio.run(srv.rest_add_fact(FakeRequest({"text": "A novel verified finding about vault health", "user_id": "amadeus"})))
    body = json.loads(resp.body)
    check(
        "POST /facts novel fact stores with a fresh id",
        resp.status_code == http_ok and body.get("ok") is True and body.get("id") and body.get("id") != existing["id"],
    )

    print("\nwrite-dedupe: all checks passed")


if __name__ == "__main__":
    main()
