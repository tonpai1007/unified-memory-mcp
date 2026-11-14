"""Optional supervisor for long-lived runs.

MCP stdio servers are spawned per client session, which is why this package
serves over HTTP instead. When it serves over HTTP, something should keep it
alive: this watchdog probes the port and restarts the server if it dies.

    python -m unified_memory.watchdog

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
# Optional extra services to supervise alongside the memory server, e.g. a
# companion gateway on another port:
#   EXTRA_SERVICES="Name|port|command|Name2|port2|command2"
# The command may contain spaces; only the first two fields are split on "|".
EXTRA_SERVICES = os.environ.get("AMADEUS_WATCHDOG_EXTRA_SERVICES", "")

# Crash-loop breaker: if the same service dies more than this many times in
# CRASH_WINDOW seconds, stop restarting it and scream — the old behaviour
# thrashed forever, each restart costing 8-27 s of cold warm-up.
CRASH_WINDOW_SECONDS = float(os.environ.get("AMADEUS_WATCHDOG_CRASH_WINDOW", "600"))
CRASH_LIMIT = int(os.environ.get("AMADEUS_WATCHDOG_CRASH_LIMIT", "5"))
LOG_PATH = Path(os.environ.get("TEMP", str(Path.home()))) / "unified_memory_watchdog.log"
MAX_LOG_BYTES = 1024 * 1024

_last_started: dict[str, float] = {}
# Live child handles per service, used to surface exit codes on failure
_spawned: dict[str, subprocess.Popen] = {}
# Timestamps of recent restarts per service, for the crash-loop breaker
_restart_history: dict[str, list[float]] = {}


def _exit_code(name: str) -> int | None:
    proc = _spawned.get(name)
    if proc is None:
        return None
    try:
        return proc.poll()
    except Exception:
        return None


def _capture_child_tail(name: str, lines: int = 40) -> list[str]:
    """Read the last lines of the child's stderr/stdout log into the watchdog log.

    The server writes to unified_memory_server.log; when it dies we can't afford to
    lose the evidence — the watchdog log is where every operator starts."""
    log = Path(os.environ.get("TEMP", str(Path.home()))) / "unified_memory_server.log"
    if not log.exists():
        return [f"watchdog: (no child log at {log})"]
    try:
        content = log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"watchdog: (unreadable child log: {exc})"]
    tail = content.splitlines()[-lines:]
    return [f"watchdog: {name} exited key snippet:"] + [f"  {line}" for line in tail]


def _recent_restarts(name: str) -> int:
    now = time.time()
    history = [t for t in _restart_history.get(name, []) if now - t < CRASH_WINDOW_SECONDS]
    _restart_history[name] = history
    return len(history)


def _note_restart(name: str) -> None:
    _restart_history.setdefault(name, []).append(time.time())


def _should_restart(name: str) -> bool:
    return _recent_restarts(name) < CRASH_LIMIT


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


# RSS check moved to the server side (rss.py) — the watchdog's tracked
# subprocess is the venv-python wrapper, not the real worker the user feels.
def terminate_service(name: str) -> bool:
    proc = _spawned.get(name)
    if proc is None:
        return False
    try:
        if proc.poll() is not None:
            return True  # already dead
        proc.terminate()
        try:
            proc.wait(timeout=8)
            return True
        except Exception:
            proc.kill()
            return True
    except Exception as exc:
        log(f"watchdog: terminate({name}) failed: {exc!r}")
        return False


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
    log_path = Path(os.environ.get("TEMP", str(Path.home()))) / "unified_memory_server.log"
    try:
        handle = log_path.open("a", encoding="utf-8")
    except OSError:
        handle = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "unified_memory.server"],
            env=env,
            stdout=handle,
            stderr=handle,
            creationflags=creationflags,
        )
        _spawned["server"] = proc
        _note_restart("server")
        log(f"watchdog: started amadeus-memory on {HOST}:{PORT} (pid {proc.pid}, log: {log_path})")
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


def _extra_services() -> list[dict]:
    """Parse EXTRA_SERVICES into supervised service descriptors.

    Format: ``Name|port|command`` repeated, separated by further pipes. The
    command is the last field of each triple, so it may contain spaces.
    """
    services: list[dict] = []
    fields = [f for f in EXTRA_SERVICES.split("|") if f != ""]
    index = 0
    while index + 2 < len(fields):
        name = fields[index]
        try:
            port = int(fields[index + 1])
        except ValueError:
            break
        command = fields[index + 2]
        services.append({"name": name, "port": port, "command": command, "cwd": os.getcwd()})
        index += 3
    return services


def start_extra(service: dict) -> bool:
    """Start one extra service if its port is not answering."""
    if port_alive(service["port"]):
        return False
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            service["command"],
            cwd=service.get("cwd") or os.getcwd(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        _spawned[service["name"]] = proc
        _note_restart(service["name"])
        log(f"watchdog: started {service['name']} on {HOST}:{service['port']} (pid {proc.pid})")
        _last_started[service["name"]] = time.time()
        return True
    except Exception as exc:
        log(f"watchdog: failed to start {service['name']}: {exc!r}")
        return False


def main() -> int:
    log(f"watchdog: started (pid {os.getpid()}) monitoring {HOST}:{PORT}")
    if start_server():
        time.sleep(10)
    extras = _extra_services()
    for service in extras:
        if start_extra(service):
            time.sleep(5)
    if REFRESH_MINUTES > 0 and REFRESH_TARGETS:
        threading.Thread(target=_refresh_loop, name="cbm-refresh", daemon=True).start()
        log(f"watchdog: graph refresher every {REFRESH_MINUTES:.0f} min -> {REFRESH_TARGETS}")
    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            if not port_alive(PORT):
                if (time.time() - _last_started.get("server", 0.0)) < START_GRACE_SECONDS:
                    continue  # still booting
                if not _should_restart("server"):
                    recent = _recent_restarts("server")
                    log(
                        f"watchdog: amadeus-memory not answering; exit_code={_exit_code('server')}; "
                        f"{recent} restarts in {CRASH_WINDOW_SECONDS:.0f}s — STOPPING supervision. "
                        "Fix the server (see unified_memory_server.log) or raise AMADEUS_WATCHDOG_CRASH_LIMIT."
                    )
                    time.sleep(CHECK_INTERVAL)
                    continue
                log(
                    f"watchdog: amadeus-memory not answering; exit_code={_exit_code('server')}; "
                    f"restarts in window: {_recent_restarts('server') + 1} -> restarting"
                )
                for line in _capture_child_tail("server"):
                    log(line)
                start_server()
                continue

            for service in extras:
                if not port_alive(service["port"]):
                    if (time.time() - _last_started.get(service["name"], 0.0)) < START_GRACE_SECONDS:
                        continue
                    if not _should_restart(service["name"]):
                        recent = _recent_restarts(service["name"])
                        log(
                            f"watchdog: {service['name']} not answering; exit_code={_exit_code(service['name'])}; "
                            f"{recent} restarts in {CRASH_WINDOW_SECONDS:.0f}s — STOPPING supervision."
                        )
                        continue
                    log(f"watchdog: {service['name']} not answering; exit_code={_exit_code(service['name'])} -> restarting")
                    start_extra(service)
        except KeyboardInterrupt:
            log("watchdog: interrupted")
            return 0
        except Exception as exc:
            log(f"watchdog: loop error {exc!r}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
