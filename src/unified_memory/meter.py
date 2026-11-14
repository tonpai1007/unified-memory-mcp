"""Objective metering for the "cut re-discovery tokens" contract.

Every graph-backed tool records:
  - response bytes it handed to the agent
  - baseline bytes  a naive agent would have paid to answer the same
    question by reading the source files whose symbols the call returned
  - the arithmetic difference is the headline: memory_savings reports it.

Tools that have no file-scoped baseline (fact fetches, writes) are counted
for calls/bytes but do not accrue a baseline, and are reported as such.

Persistence is best-effort: counters live in the fact store directory so
restarts keep the running total. Writers never block reads; failures to
persist are swallowed (the meter must never break the server).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


class Meter:
    def __init__(self, path: str | None):
        self._path = Path(path) if path else None
        self._data = {
            "since": time.time(),
            "tools": {},
            "dedup_hits": 0,
        }
        if self._path and self._path.exists():
            try:
                prev = json.loads(self._path.read_text(encoding="utf-8"))
                prev.setdefault("tools", {})
                self._data["tools"] = prev["tools"]
                self._data["dedup_hits"] = prev.get("dedup_hits", 0)
            except Exception:
                pass  # corrupt file — start fresh, never crash for bookkeeping

    # -- ingest ---------------------------------------------------------------
    def record(self, tool: str, response_bytes: int, baseline_bytes: int | None = None) -> None:
        with _LOCK:
            entry = self._data["tools"].setdefault(
                tool, {"calls": 0, "response_bytes": 0, "baseline_bytes": 0}
            )
            entry["calls"] += 1
            entry["response_bytes"] += response_bytes
            if baseline_bytes:
                entry["baseline_bytes"] += baseline_bytes
            self._persist()

    def record_dedup_hit(self) -> None:
        with _LOCK:
            self._data["dedup_hits"] = self._data.get("dedup_hits", 0) + 1
            self._persist()

    # -- reporting --------------------------------------------------------------
    def report(self) -> dict:
        with _LOCK:
            tools = {}
            saved_total = 0
            for name, entry in self._data["tools"].items():
                baseline = entry.get("baseline_bytes", 0)
                response = entry.get("response_bytes", 0)
                saved = max(0, baseline - response) if baseline else 0
                saved_total += saved
                tools[name] = {
                    **entry,
                    "saved_bytes": saved,
                    "saved_tokens_est": saved // 4,
                }
            calls = sum(e.get("calls", 0) for e in self._data["tools"].values())
            response_total = sum(e.get("response_bytes", 0) for e in self._data["tools"].values())
            return {
                "uptime_seconds": int(time.time() - self._data["since"]),
                "tools": tools,
                "totals": {
                    "calls": calls,
                    "response_bytes": response_total,
                    "saved_bytes": saved_total,
                    "saved_tokens_est": saved_total // 4,
                },
                "dedup_hits": self._data.get("dedup_hits", 0),
                "note": (
                    "tokens saved ~= bytes/4 vs the naive cost of reading the source files "
                    "whose symbols the call returned. Only graph-backed tools carry a "
                    "baseline estimate. Fact fetches/writes count calls and bytes only."
                ),
            }

    # -- persistence ------------------------------------------------------------
    def _persist(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"since": self._data["since"], "tools": self._data["tools"], "dedup_hits": self._data["dedup_hits"]}),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            pass  # bookkeeping is valuable but never worth a crash


_meter: Meter | None = None
_meter_lock = threading.Lock()


def get_meter() -> Meter:
    global _meter
    if _meter is None:
        with _meter_lock:
            if _meter is None:
                try:
                    from .store import store_dir

                    root = store_dir()
                    path = str(Path(root) / "memory_savings.json") if root else None
                except Exception:
                    path = None
                _meter = Meter(path)
    return _meter
