"""Portability of the code-graph layer: the MCP must work on any indexed codebase.

Covers the caller-aware default project (the server's own CWD never changes,
so a codebase-aware default can only come from the caller's _meta hint or an
explicit ``project=``) and the ``graph_projects`` discovery tool.

Run:  python tests/test_project_dynamic.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ["MEM0_DIR"] = tempfile.mkdtemp(prefix="unified-memory-projects-")
os.environ.pop("CODEBASE_MEMORY_PROJECT", None)
os.environ.pop("CODEBASE_MEMORY_EXE", None)

from unified_memory import server as srv  # noqa: E402

PROJECTS = [
    {"name": "C-AMADEUS", "root_path": "C:/AMADEUS"},
    {"name": "C-Augustwater", "root_path": "C:/Augustwater"},
    {"name": "C-TONPAIICE3", "root_path": "C:/TONPAIICE3"},
    {"name": "C-codework-computational-machine-design", "root_path": "C:/codework/computational-machine-design"},
]


class Ctx:
    """MCP Context stand-in whose _meta extras carry client hints."""

    def __init__(self, extras: dict | None = None) -> None:
        self.request_context = SimpleNamespace(
            meta=SimpleNamespace(model_extra=extras or {})
        )


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "ok " if ok else "FAIL"
    print(f"  [{mark}] {label}{' — ' + detail if detail else ''}")
    if not ok:
        raise SystemExit(f"project-dynamic check failed: {label}")


async def fake_graph(tool: str, args: dict) -> str:
    calls.append(tool)
    if tool == "list_projects":
        return json.dumps({"projects": PROJECTS, "total": len(PROJECTS)})
    return json.dumps({})


async def reset_state() -> None:
    srv._default_project_cache = None
    srv._PROJECT_LIST_CACHE = None
    srv._project_list_cache_at = 0.0


async def main() -> None:
    global calls
    calls = []
    srv.graph_mod.call = fake_graph  # type: ignore[attr-defined]

    # 1. cwd hint extraction from _meta
    ctx = Ctx({"ai.opencode/sessionID": "ses_x", "ai.opencode/directory": "C:\\Augustwater"})
    check("_cwd_hint_from reads the caller's directory", srv._cwd_hint_from(ctx) == "C:\\Augustwater")
    check("empty _meta yields no hint", srv._cwd_hint_from(Ctx({})) is None)
    check("broken ctx yields no hint", srv._cwd_hint_from(None) is None)

    await reset_state()

    # 2. directory → project matching
    check(
        "exact root-path match wins (case/separator insensitive)",
        srv._project_for_directory("c:\\amadeus\\", PROJECTS) == "C-AMADEUS",
    )
    check(
        "name-suffix match finds nested codebases",
        srv._project_for_directory("C:\\codework\\computational-machine-design", PROJECTS)
        == "C-codework-computational-machine-design",
    )
    check("unknown directory matches nothing", srv._project_for_directory("C:\\elsewhere", PROJECTS) is None)

    # 3. default resolution: cwd hint beats the static fallback
    got = await srv._default_project("C:\\Augustwater")
    check("cwd hint resolves to the caller's project", got == "C-Augustwater", f"(got {got})")

    # 4. env override still wins over everything
    os.environ["CODEBASE_MEMORY_PROJECT"] = "C-TONPAIICE3"
    got = await srv._default_project("C:\\Augustwater")
    check("env override beats the cwd hint", got == "C-TONPAIICE3", f"(got {got})")
    os.environ.pop("CODEBASE_MEMORY_PROJECT")

    # 5. no hint → static fallback does not crash and yields *a* project
    await reset_state()
    got = await srv._default_project(None)
    check("static fallback yields a project", bool(got), f"(got {got!r})")

    # 6. graph_projects discovery tool lists everything
    await reset_state()
    payload = json.loads(await srv.graph_projects())
    check(
        "graph_projects lists indexed projects with root paths",
        payload["total"] == len(PROJECTS)
        and payload["projects"][1]["name"] == "C-Augustwater"
        and payload["projects"][1]["root_path"] == "C:/Augustwater",
    )

    print("\nproject-dynamic: all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
