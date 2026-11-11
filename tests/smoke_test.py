"""Smoke test for amadeus-memory-mcp.

Checks that
  * the package imports and exposes the expected tool surface,
  * the fact store works end to end (add -> search -> delete) against a
    throwaway store directory,
  * the code-graph layer degrades gracefully when codebase-memory is absent
    (it must NOT take the fact side down with it).

Run:  python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Use a throwaway store so a test run never touches a real memory store.
os.environ["MEM0_DIR"] = tempfile.mkdtemp(prefix="amadeus-memory-smoke-")
os.environ.pop("OPENAI_API_KEY", None)

from unified_memory import server as srv  # noqa: E402

EXPECTED_TOOLS = {
    "add_memory",
    "search_memories",
    "get_memories",
    "get_memory",
    "update_memory",
    "delete_memory",
    "delete_all_memories",
    "remember_fact",
    "recall",
    "facts_for_symbol",
    "fact_freshness",
    "fact_impact",
    "facts_by_project",
    "expired_facts",
    "search_graph",
    "trace_path",
    "get_code_snippet",
    "query_graph",
    "get_architecture",
    "check_index_coverage",
    "detect_changes",
    "list_projects",
    "index_status",
    "index_repository",
    "delete_project",
    "codebase_memory",
    "system_vitals",
    "memory_audit",
    "memory_scope_facts",
    "memory_purge_expired",
}


def _text(result) -> str:
    """Extract the text payload from a FastMCP call result.

    The SDK returns either a dict (structured), a tuple of
    (content_blocks, structured) or a list of content blocks.
    """
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        blocks = result.get("content") or []
        if blocks:
            return str(getattr(blocks[0], "text", blocks[0]))
        return json.dumps(result)
    if isinstance(result, list):
        return str(getattr(result[0], "text", result[0])) if result else ""
    return str(result)


def _json(result) -> dict:
    raw = _text(result)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def main() -> int:
    failures: list[str] = []

    tools = await srv.mcp.list_tools()
    names = {tool.name for tool in tools}
    missing = EXPECTED_TOOLS - names
    if missing:
        failures.append(f"missing tools: {sorted(missing)}")
    else:
        print(f"OK  tool surface: {len(names)} tools")

    # Fact store round trip.
    payload = _json(
        await srv.mcp.call_tool(
            "add_memory",
            {
                "text": "SMOKE TEST: fact store round trip",
                "user_id": "smoke",
                "project": "SMOKE",
            },
        )
    )
    stored = (payload.get("results") or [{}])[0].get("id")
    if not stored:
        failures.append("add_memory returned no id")
    else:
        print("OK  add_memory")

    found = _json(
        await srv.mcp.call_tool(
            "search_memories",
            {"query": "round trip", "user_id": "smoke", "top_k": 5},
        )
    )
    if not (found.get("results") or []):
        failures.append("search_memories found nothing")
    else:
        print("OK  search_memories")

    # TTL annotation must be present on reads.
    rows = found.get("results") or []
    if rows and "ttl" not in rows[0]:
        failures.append("search results are missing the ttl annotation")
    else:
        print("OK  ttl annotation")

    scoped = _json(
        await srv.mcp.call_tool(
            "search_memories",
            {"query": "round trip", "user_id": "smoke", "top_k": 5, "project": "OTHER"},
        )
    )
    if scoped.get("dropped_other_project", 0) < 1:
        failures.append("project scoping did not drop the other-project fact")
    else:
        print("OK  project scoping")

    deleted = _json(await srv.mcp.call_tool("delete_memory", {"memory_id": stored}))
    if not deleted:
        failures.append("delete_memory returned nothing")
    else:
        print("OK  delete_memory")

    # Graph layer: must degrade gracefully (no crash) when the binary is absent.
    graph_text = _text(await srv.mcp.call_tool("list_projects", {}))
    if "error" in graph_text.lower():
        print("OK  graph layer degrades gracefully (binary absent)")
    else:
        print("OK  graph layer answered")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    sys.stderr.flush()
    # qdrant-client's local mode closes its store from __del__ during
    # interpreter shutdown, where sys.meta_path is already gone — that raises
    # an "Exception ignored" traceback and can flip the exit code. The test
    # result is already decided, so exit hard and skip interpreter teardown.
    os._exit(code)
