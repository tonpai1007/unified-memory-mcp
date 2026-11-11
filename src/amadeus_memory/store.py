"""Fact store access (mem0).

Two interchangeable backends, same observable behaviour:

1. **upstream module** — when the ``mem0-mcp-server`` package (Apache-2.0,
   https://github.com/zfy258/mem0-mcp-server) is installed, its ``mcp_server``
   module is used verbatim. That keeps the store layout, the ``history.db``
   bookkeeping and the embedder config byte-identical to what other tools in
   the same ecosystem expect.
2. **direct mem0ai** — otherwise the same store is built here with mem0ai
   directly (embedded Qdrant + the same history db path), so the package works
   standalone.

The store root comes from ``MEM0_DIR`` (default: ``~/.amadeus-memory``).

Embedding note: the local collection is 512-dim FastEmbed. ``OPENAI_API_KEY``
is deliberately unset so the embedder can never silently switch to 1536-dim
OpenAI embeddings, which would corrupt an existing collection.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

DEFAULT_STORE_DIR = Path.home() / ".amadeus-memory"


def store_dir() -> Path:
    return Path(os.environ.get("MEM0_DIR") or DEFAULT_STORE_DIR)


def _ensure_env() -> None:
    os.environ.setdefault("MEM0_DIR", str(store_dir()))
    # 512-dim FastEmbed collection — never let an OpenAI key switch it.
    os.environ.pop("OPENAI_API_KEY", None)


def _upstream_module():
    """Return the upstream local-store module if it is usable.

    Two distributions exist and they are NOT interchangeable:
      * the git build (https://github.com/zfy258/mem0-mcp-server, module
        ``mcp_server``) — a LOCAL store (embedded Qdrant + history db) with
        a ``call_tool`` dispatcher; this is the one we mirror;
      * the PyPI build (0.2.x, module ``mem0_mcp_server.server``) — a HOSTED
        mem0 API client (needs an API key, no local store, no call_tool).
    The hosted variant is therefore ignored and the direct mem0ai path is used
    instead.
    """
    try:
        import mcp_server  # type: ignore
    except ModuleNotFoundError:
        return None
    if not hasattr(mcp_server, "call_tool"):
        return None
    return mcp_server


def _build_direct_memory():
    """Build a mem0 Memory instance with the same layout as the upstream server."""
    from mem0 import Memory

    mem0_dir = store_dir()
    config = {
        "llm": {"provider": "openai", "config": {"model": "gpt-5-mini", "api_key": "dummy"}},
        "embedder": {"provider": "fastembed", "config": {"model": "BAAI/bge-small-en-v1.5"}},
        "vector_store": {
            "provider": "qdrant",
            "config": {"path": str(mem0_dir / "qdrant"), "embedding_model_dims": 512},
        },
        "history_db_path": str(mem0_dir / "history.db"),
    }
    memory = Memory.from_config(config)
    # Load the model now so a slow first start happens here, not on first call.
    try:
        memory.embedding_model.dense_model
    except Exception:  # noqa: BLE001
        pass
    return memory


_DIRECT_MEMORY = None


def _direct_call_tool(name: str, args: dict[str, Any]) -> Any:
    """mem0 tool semantics, mirroring the upstream MCP server."""
    global _DIRECT_MEMORY
    if _DIRECT_MEMORY is None:
        _DIRECT_MEMORY = _build_direct_memory()
    memory = _DIRECT_MEMORY
    args = args or {}

    if name == "add_memory":
        text = args.get("text")
        if not text:
            raise ValueError("add_memory requires 'text'")
        return memory.add(
            [{"role": "user", "content": text}],
            user_id=args.get("user_id") or "default_user",
            agent_id=args.get("agent_id"),
            metadata=args.get("metadata"),
            infer=bool(args.get("infer", False)),
        )
    if name == "search_memories":
        query = args.get("query")
        if not query:
            raise ValueError("search_memories requires 'query'")
        return memory.search(
            query,
            filters=_filters(args.get("user_id"), args.get("agent_id")),
            top_k=int(args.get("top_k", 10)),
            threshold=float(args.get("threshold", 0.1)),
        )
    if name == "get_memories":
        return memory.get_all(
            filters=_filters(args.get("user_id"), args.get("agent_id")),
            top_k=int(args.get("top_k", 20)),
        )
    if name == "get_memory":
        return memory.get(args.get("memory_id"))
    if name == "update_memory":
        return memory.update(args.get("memory_id"), text=args.get("text"), metadata=args.get("metadata"))
    if name == "delete_memory":
        return memory.delete(args.get("memory_id"))
    if name == "delete_all_memories":
        user_id = args.get("user_id")
        agent_id = args.get("agent_id")
        if not user_id and not agent_id:
            raise ValueError("delete_all_memories requires 'user_id' or 'agent_id'")
        return memory.delete_all(user_id=user_id, agent_id=agent_id)
    raise ValueError(f"Unknown tool: {name}")


def _filters(user_id: str | None = None, agent_id: str | None = None) -> dict[str, str]:
    filters: dict[str, str] = {}
    if user_id:
        filters["user_id"] = user_id
    if agent_id:
        filters["agent_id"] = agent_id
    if not filters:
        filters["user_id"] = "default_user"
    return filters


def call_tool(name: str, args: dict[str, Any]) -> Any:
    """Call a mem0 tool by name.

    Uses the upstream module when present, otherwise the direct mem0ai path.
    """
    _ensure_env()
    upstream = _upstream_module()
    if upstream is not None:
        return upstream.call_tool(name, args)
    return _direct_call_tool(name, args)


def backend() -> str:
    """Which backend is in use: ``upstream`` or ``direct``."""
    return "upstream" if _upstream_module() is not None else "direct"
