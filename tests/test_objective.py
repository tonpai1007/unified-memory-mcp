"""Guard the objective: agents must never pay twice for the same answer.

These tests pin the contract that matters — recall stays small and honest,
and the savings ledger records what graph-backed calls spared you.

No binary required: the graph layer is monkeypatched with a deterministic
fixture, so CI and fresh checkouts run the same assertions.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from unified_memory import graph as graph_mod
from unified_memory import server as srv

FIXTURE_GRAPH_TEXT = (
    "lib.target.mineFunction (pkg/lib.py):\n"
    "  mineFunction Function in pkg/lib.py\n"
    "class.target (pkg/lib.py):\n"
    "  target Class in pkg/lib.py\n"
)
FIXTURE_INDEX_STATUS = "project: fixture\nstatus: skipped 7 paths (index refresh pending)\n"


def _run(coro):
    return asyncio.run(coro)


async def _fake_graph_call(tool, args, **kwargs):
    if tool == "search_graph":
        return FIXTURE_GRAPH_TEXT
    if tool == "index_status":
        return FIXTURE_INDEX_STATUS
    return "{}"


def _facts_fixture(*_args, **_kwargs):
    return {
        "results": [
            {
                "id": "m1",
                "memory": "run `python -m pytest` before pushing; CI is stricter locally",
                "score": 0.9,
                "metadata": {
                    "symbols": [
                        {
                            "symbol": "mineFunction",
                            "qualified_name": "lib.target.mineFunction",
                            "file": "pkg/lib.py",
                        }
                    ]
                },
                "ttl": None,
            }
        ],
        "dropped_other_project": 0,
        "unscoped_kept": 0,
        "expired": 0,
    }


def test_recall_returns_bounded_honest_payload(monkeypatch):
    monkeypatch.setattr(graph_mod, "call", _fake_graph_call)
    monkeypatch.setattr(srv, "_search_facts", _facts_fixture)

    payload = _run(srv.recall("mineFunction", SimpleNamespace(), project="fixture"))
    data = json.loads(payload)

    assert data["project"] == "fixture"
    assert data["facts"][0]["id"] == "m1"
    assert any(row["name"] == "mineFunction" for row in data["code"])
    # agents must see index health, not a blind answer
    assert data["coverage"]["refresh_may_help"] is True
    assert "skipped 7 paths" in data["coverage"]["note"]
    assert "stale_linked_facts" in data
    # the whole point: bounded response bytes
    assert len(payload.encode("utf-8")) < 2000


def test_memory_savings_counts_recall(monkeypatch):
    monkeypatch.setattr(graph_mod, "call", _fake_graph_call)
    monkeypatch.setattr(srv, "_search_facts", _facts_fixture)

    before = json.loads(_run(srv.memory_savings()))
    calls_before = before["totals"]["calls"]

    _run(srv.recall("mineFunction", SimpleNamespace(), project="fixture"))
    report = json.loads(_run(srv.memory_savings()))

    assert report["totals"]["calls"] >= calls_before + 1
    assert "recall" in report["tools"]
    assert report["tools"]["recall"]["calls"] >= 1
    assert "dedup_hits" in report
    # noise tolerance: the assertion is about presence, not exact byte counts
    assert report["totals"]["saved_tokens_est"] >= 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
