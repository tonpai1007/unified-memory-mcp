"""Optional supervisor for long-lived runs.

MCP stdio servers are spawned per client session, which is why this package
serves over HTTP instead. When it serves over HTTP, something should keep it
alive: this watchdog probes the port and restarts the server if it dies.

    python -m amadeus_memory.watchdog

It also keeps the code graph fresh on a schedule (codebase-memory's own file
watcher is usually disabled, because a large dirty worktree makes every query
wait for a rebuild): the refresh runs in the background and drives the
server's own ``index_repository`` tool, so the index is rebuilt in place by the
process that already holds it.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HOST = os.environ.get("MEM0_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEM0_PORT", "8777"))
CHECK_INTERVAL = float(os.environ.get("AMADEUS_WATCHDOG_INTERVAL", "45"))
# A service that was just spawned needs time to bind (the server prewarms the
# graph index and the embedding model first). Without this grace period the
# probe would kill and respawn a service that is still booting.
START_GRACE_SECONDS = float(os.environ.get("AMADEUS_WATCHDOG_GRACE_SECONDS", "120"))
REFRESH_MINUTES = float(os.environ.get("CODEBASE_MEMORY_REFRESH_MINUTES", "0"))
REFRESH_TARGETS = os.environ.get("CODEBASE_MEMORY_REFRESH_TARGETS", "")
REFRESH_MODE = os.environ.get("CODEBASE_MEMORY_REFRESH_MODE", "fast")
LOG_PATH = Path(os.environ.get("TEMP", str(Path.home()))) / "amadeus_memory_watchdog.log"
MAX_LOG_BYTES = 1024 * 1024

_last_started: dict[str, float] = {}


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            trimmed = LOG_PATH.read_text(encoding="utf-8", errors="replace")[-MAX_LOG_BYTES // 2 :]
            LOG_PATH.write_text(trimmed, encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def port_alive(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=2):
            return True
    except OSError:
        return False


def start_server() -> bool:
    if port_alive(PORT):
        return False
    env = dict(os.environ)
    env.setdefault("MEM0_PORT", str(PORT))
    env.setdefault("MEM0_HOST", HOST)
    env.pop("OPENAI_API_KEY", None)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    log_path = Path(os.environ.get("TEMP", str(Path.home()))) / "amadeus_memory_server.log"
    try:
        handle = log_path.open("a", encoding="utf-8")
    except OSError:
        handle = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, "-m", "amadeus_memory.server"],
            env=env,
            stdout=handle,
            stderr=handle,
            creationflags=creationflags,
        )
        log(f"watchdog: started amadeus-memory on {HOST}:{PORT} (log: {log_path})")
        _last_started["server"] = time.time()
        return True
    except Exception as exc:
        log(f"watchdog: failed to start server: {exc!r}")
        return False
    finally:
        if hasattr(handle, "close"):
            handle.close()


async def _refresh_once() -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = f"http://{HOST}:{PORT}/mcp"
    async with streamablehttp_client(url) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        for raw_entry in REFRESH_TARGETS.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            name, _, repo_path = entry.partition("=")
            name = name.strip()
            repo_path = repo_path.strip()
            started = time.monotonic()
            try:
                await session.call_tool(
                    "index_repository",
                    {"repo_path": repo_path, "mode": REFRESH_MODE, "name": name},
                )
                log(f"watchdog: re-indexed {name} in {time.monotonic() - started:.0f}s")
            except Exception as exc:
                log(f"watchdog: re-index of {name} failed: {exc!r}")


def _refresh_loop() -> None:
    time.sleep(120)
    while True:
        try:
            asyncio.run(_refresh_once())
        except Exception as exc:
            log(f"watchdog: graph refresh failed: {exc!r}")
        time.sleep(max(60.0, REFRESH_MINUTES * 60.0))


def main() -> int:
    log(f"watchdog: started (pid {os.getpid()}) monitoring {HOST}:{PORT}")
    if start_server():
        time.sleep(10)
    if REFRESH_MINUTES > 0 and REFRESH_TARGETS:
        threading.Thread(target=_refresh_loop, name="cbm-refresh", daemon=True).start()
        log(f"watchdog: graph refresher every {REFRESH_MINUTES:.0f} min -> {REFRESH_TARGETS}")
    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            if not port_alive(PORT):
                if (time.time() - _last_started.get("server", 0.0)) < START_GRACE_SECONDS:
                    continue  # still booting
                log("watchdog: amadeus-memory not answering -> restarting")
                start_server()
        except KeyboardInterrupt:
            log("watchdog: interrupted")
            return 0
        except Exception as exc:
            log(f"watchdog: loop error {exc!r}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
