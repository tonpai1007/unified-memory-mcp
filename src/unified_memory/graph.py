"""Code knowledge graph access (codebase-memory).

The graph is an **external, optional** dependency: ``codebase-memory-mcp``
(https://github.com/DeusData/codebase-memory-mcp) is a separate project and is
NOT vendored here. This module locates it, keeps ONE warm child process alive
and forwards tool calls to it. When the binary is not installed, every graph
tool degrades to a clear error instead of failing the server.

Why a warm child instead of the CLI
------------------------------------
``codebase-memory-mcp cli <tool> --args-file`` spawns the binary per call and
reloads the whole index from disk every time â€” measured 7.4-8.5 s per query.
One long-lived child holds the index in RAM: queries drop to tens of
milliseconds.

Why the child lives in its own thread
-------------------------------------
The child's stdio session cannot be owned by a request handler: anyio's
``stdio_client`` raises *"Attempted to exit a cancel scope in a different task
than it was entered in"* when a request tears it down, and stateless HTTP
makes every request its own task. A dedicated thread with its own event loop
sidesteps that entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_PROJECT = os.environ.get("CODEBASE_MEMORY_PROJECT", "")


def codebase_memory_exe() -> str | None:
    """Locate the codebase-memory binary: env override, PATH, install dirs."""
    override = os.environ.get("CODEBASE_MEMORY_EXE")
    if override and Path(override).exists():
        return override
    found = shutil.which("codebase-memory-mcp")
    if found:
        return found
    candidates = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            str(Path(local) / "Programs" / "codebase-memory-mcp" / "codebase-memory-mcp.exe") if local else "",
            r"C:\Program Files\codebase-memory-mcp\codebase-memory-mcp.exe",
        ]
    else:
        candidates += [
            str(Path.home() / ".local" / "bin" / "codebase-memory-mcp"),
            "/usr/local/bin/codebase-memory-mcp",
            "/opt/homebrew/bin/codebase-memory-mcp",
        ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _log(message: str) -> None:
    print(f"[amadeus-memory] {message}", file=sys.stderr, flush=True)


class CodeGraph:
    """A warm codebase-memory child driven from its own thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._exe = codebase_memory_exe()

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = 90.0) -> bool:
        if not self._exe:
            self._error = FileNotFoundError("codebase-memory-mcp binary not found")
            self._ready.set()
            return False
        self._thread = threading.Thread(target=self._run, name="cbm-warm-child", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout=timeout)

    def _run(self) -> None:
        async def _main() -> None:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            self._loop = asyncio.get_running_loop()
            params = StdioServerParameters(command=str(self._exe), args=[])
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.5)

        try:
            asyncio.run(_main())
        except BaseException as exc:
            self._error = exc
            self._ready.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)

    # -- calls -------------------------------------------------------------
    def call(self, tool: str, args: dict[str, Any], timeout: float = 180.0) -> str:
        if self._session is None or self._loop is None:
            raise RuntimeError(f"codebase-memory child unavailable: {self._error!r}")
        future = asyncio.run_coroutine_threadsafe(self._session.call_tool(tool, args), self._loop)
        result = future.result(timeout=timeout)
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False, default=str)
        texts = [getattr(c, "text", "") for c in (result.content or [])]
        return "\n".join(t for t in texts if t)

    # -- CLI fallback ------------------------------------------------------
    def call_cli(self, tool: str, args: dict[str, Any], timeout: float = 180.0) -> str:
        """Fallback path: run one tool through the CLI (slower, no warm index)."""
        if not self._exe:
            raise FileNotFoundError("codebase-memory-mcp binary not found")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump({k: v for k, v in (args or {}).items() if v is not None}, fh)
            args_path = fh.name
        out_path = err_path = None
        try:
            out_file = tempfile.NamedTemporaryFile("w+", suffix=".out", delete=False, encoding="utf-8")
            err_file = tempfile.NamedTemporaryFile("w+", suffix=".err", delete=False, encoding="utf-8")
            out_path, err_path = out_file.name, err_file.name
            try:
                proc = subprocess.run(
                    [str(self._exe), "cli", tool, "--args-file", args_path],
                    stdout=out_file,
                    stderr=err_file,
                    timeout=timeout,
                    check=False,
                )
            finally:
                out_file.close()
                err_file.close()
            with open(out_path, encoding="utf-8", errors="replace") as fh:
                out = fh.read().strip()
            with open(err_path, encoding="utf-8", errors="replace") as fh:
                err = fh.read().strip()
            if not out:
                raise RuntimeError(f"codebase-memory {tool}: no output (rc={proc.returncode}): {err[-400:]}")
            return out
        finally:
            for tmp in (args_path, out_path, err_path):
                if tmp:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)


_graph: CodeGraph | None = None
_graph_lock = asyncio.Lock()


async def get_graph() -> CodeGraph:
    """Return the warm graph child, starting it if needed."""
    global _graph
    if _graph is not None:
        return _graph
    graph = CodeGraph()
    if not graph.start():
        _log(f"graph child unavailable ({graph._error!r}) â€” graph tools will error, facts still work")
    _graph = graph
    return graph


async def reset_graph() -> None:
    global _graph
    graph, _graph = _graph, None
    if graph is not None:
        await asyncio.to_thread(graph.stop)


async def call(tool: str, args: dict[str, Any]) -> str:
    """Call a graph tool; returns the raw text payload (JSON when applicable)."""
    async with _graph_lock:
        graph = await get_graph()
        last_error = ""
        for attempt in (1, 2):
            try:
                return await asyncio.to_thread(graph.call, tool, args)
            except Exception as exc:
                last_error = repr(exc)
                if attempt == 1:
                    await reset_graph()
                    continue
        # Warm child unavailable â€” fall back to the CLI (slow but works).
        try:
            return await asyncio.to_thread(graph.call_cli, tool, args)
        except Exception as exc:
            return json.dumps({"error": f"codebase-memory {tool} failed: {last_error} / cli: {exc!r}"})


async def prewarm() -> None:
    """Pay the cold costs (index load, embedding model) at startup."""
    started = time.monotonic()
    try:
        graph = await get_graph()
        await asyncio.to_thread(graph.call, "list_projects", {})
        if DEFAULT_PROJECT:
            await asyncio.to_thread(graph.call, "index_status", {"project": DEFAULT_PROJECT})
        _log(f"graph child warm in {time.monotonic() - started:.1f}s")
    except Exception as exc:
        _log(f"graph warmup failed: {exc!r}")
