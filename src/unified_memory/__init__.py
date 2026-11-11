"""unified-memory-mcp — one MCP server for facts AND a code knowledge graph.

Two memory surfaces used to live behind two stdio MCP servers, and MCP stdio
means the client spawns the server itself — every editor session and every
project directory got its own copy (a census on one machine showed 13+
processes and ~1 GB of RAM for the same two servers). This package serves both
surfaces from ONE process over Streamable HTTP:

  * semantic fact memory (mem0) — verified findings, decisions, gotchas
  * a code knowledge graph (codebase-memory) — symbols, callers, snippets

and, the reason for merging them, it can JOIN the two: facts are linked to the
symbols they describe, so recall returns the answer *and* the code, and a
symbol can surface the facts written about it.

Public entry points:
  * ``unified_memory.server:main``      — the MCP server (console script)
  * ``unified_memory.watchdog:main``    — optional supervisor for long-lived runs
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
