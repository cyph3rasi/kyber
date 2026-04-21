"""Shared-notebook agent tools.

These tools give every Kyber access to the same persistent key/value
store — the notebook lives on the **host** machine only. On the host,
tool execution calls :mod:`kyber.network.notebook` directly. On a spoke,
it round-trips an RPC over the WebSocket to the host, which executes
the same functions and returns the result.

All four tools are registered regardless of role:

* **standalone** instances still register them, but the tools return
  a friendly "Kyber network not configured" message so an agent that
  learned about them doesn't silently do nothing.
* **host** instances read/write the local SQLite store at
  ``~/.kyber/notebook.db``.
* **spoke** instances call :meth:`kyber.network.spoke.SpokeClient.call_rpc`
  with a 15-second timeout.

This dual-routing lives in :func:`_execute_notebook` so individual tools
stay small.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry

logger = logging.getLogger(__name__)

NOTEBOOK_TOOLSET = "network"

# Cap a few things so agents don't hit the DB too hard.
MAX_LIST_LIMIT = 200
MAX_READ_LIMIT = 50


def _current_role() -> str:
    """Return the active network role: ``host``, ``spoke``, or ``standalone``."""
    try:
        from kyber.config.loader import load_config

        cfg = load_config()
        return (cfg.network.role or "standalone").strip().lower()
    except Exception:
        return "standalone"


def _format_entry(entry: dict[str, Any]) -> str:
    """One-line summary of a notebook entry for LLM-friendly output."""
    key = entry.get("key", "")
    author = entry.get("author_name") or entry.get("author_peer_id", "")[:8]
    ts = entry.get("created_at", 0)
    import datetime as _dt

    when = (
        _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        if ts
        else "—"
    )
    tags = entry.get("tags") or []
    tag_part = f" [{', '.join(tags)}]" if tags else ""
    value = entry.get("value", "")
    preview = value if len(value) <= 300 else value[:300] + "…"
    return f"#{entry.get('id')} {key}{tag_part} · {author} · {when}\n  {preview}"


async def _execute_notebook(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run a notebook RPC locally or remotely depending on this machine's role.

    Returns the raw result dict from the host (always the same shape whether
    we went through SQLite directly or through the WebSocket).
    """
    role = _current_role()
    if role == "host":
        return await _execute_local(method, params)
    if role == "spoke":
        return await _execute_remote(method, params)
    raise RuntimeError(
        "Kyber network is not configured — run `kyber network pair` on one "
        "machine and `kyber network join` on this one to share a notebook."
    )


async def _execute_local(method: str, params: dict[str, Any]) -> dict[str, Any]:
    from kyber.network import notebook as nb
    from kyber.network.state import load_state

    state = load_state()
    author_peer_id = state.peer_id
    author_name = state.name

    name = method.split(".", 1)[1] if "." in method else method
    if name == "write":
        entry = nb.write(
            key=str(params.get("key") or ""),
            value=params.get("value", ""),
            author_peer_id=author_peer_id,
            author_name=author_name,
            tags=params.get("tags") or [],
            replace=bool(params.get("replace", False)),
        )
        return {"entry": entry.to_dict()}
    if name == "read":
        entries = nb.read(
            str(params.get("key") or ""),
            limit=int(params.get("limit", 1) or 1),
        )
        return {"entries": [e.to_dict() for e in entries]}
    if name == "list":
        entries = nb.list_entries(
            tag=params.get("tag"),
            limit=int(params.get("limit", 50) or 50),
        )
        return {"entries": [e.to_dict() for e in entries]}
    if name == "search":
        entries = nb.search(
            str(params.get("query") or ""),
            limit=int(params.get("limit", 50) or 50),
        )
        return {"entries": [e.to_dict() for e in entries]}
    raise ValueError(f"unknown notebook method: {name}")


async def _execute_remote(method: str, params: dict[str, Any]) -> dict[str, Any]:
    from kyber.network.spoke import get_spoke_client

    client = get_spoke_client()
    if not client.status.get("connected"):
        raise RuntimeError(
            "Not connected to the host. Check `kyber network status`; the "
            "link may be reconnecting — try again in a few seconds."
        )
    return await client.call_rpc(method, params, timeout=15.0)


# ── Tool definitions ─────────────────────────────────────────────────


class NotebookWriteTool(Tool):
    @property
    def name(self) -> str:
        return "notebook_write"

    @property
    def description(self) -> str:
        return (
            "Write to the shared Kyber notebook (visible on every paired "
            "machine). Use slash-namespaced keys like 'project/status'. "
            "`replace=true` upserts; default appends a new version."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string", "minLength": 1, "maxLength": 200},
                "value": {"type": "string", "description": "Note body (markdown/text/JSON)."},
                "tags": {"type": "array", "items": {"type": "string"}},
                "replace": {"type": "boolean", "description": "Upsert instead of append."},
            },
            "required": ["key", "value"],
        }

    @property
    def toolset(self) -> str:
        return NOTEBOOK_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await _execute_notebook(
                "notebook.write",
                {
                    "key": kwargs.get("key"),
                    "value": kwargs.get("value"),
                    "tags": kwargs.get("tags") or [],
                    "replace": bool(kwargs.get("replace", False)),
                },
            )
        except Exception as e:
            return f"notebook.write failed: {e}"
        entry = result.get("entry") or {}
        return (
            f"Wrote entry #{entry.get('id')} under key "
            f"{entry.get('key')!r} ({len(entry.get('value', ''))} chars)."
        )


class NotebookReadTool(Tool):
    @property
    def name(self) -> str:
        return "notebook_read"

    @property
    def description(self) -> str:
        return "Read recent notebook entries for a key (newest first)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string", "minLength": 1},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_LIMIT,
                    "description": "Versions to return. Default 1.",
                },
            },
            "required": ["key"],
        }

    @property
    def toolset(self) -> str:
        return NOTEBOOK_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await _execute_notebook(
                "notebook.read",
                {
                    "key": kwargs.get("key"),
                    "limit": min(int(kwargs.get("limit", 1) or 1), MAX_READ_LIMIT),
                },
            )
        except Exception as e:
            return f"notebook.read failed: {e}"
        entries = result.get("entries") or []
        if not entries:
            return f"No entries for key {kwargs.get('key')!r}."
        return "\n\n".join(_format_entry(e) for e in entries)


class NotebookListTool(Tool):
    @property
    def name(self) -> str:
        return "notebook_list"

    @property
    def description(self) -> str:
        return "List recent notebook entries across all keys (optionally filtered by tag)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIST_LIMIT,
                    "description": "Default 20.",
                },
            },
        }

    @property
    def toolset(self) -> str:
        return NOTEBOOK_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await _execute_notebook(
                "notebook.list",
                {
                    "tag": kwargs.get("tag"),
                    "limit": min(int(kwargs.get("limit", 20) or 20), MAX_LIST_LIMIT),
                },
            )
        except Exception as e:
            return f"notebook.list failed: {e}"
        entries = result.get("entries") or []
        if not entries:
            return "Notebook is empty."
        return "\n\n".join(_format_entry(e) for e in entries)


class NotebookSearchTool(Tool):
    @property
    def name(self) -> str:
        return "notebook_search"

    @property
    def description(self) -> str:
        return "Case-insensitive substring search over notebook keys/values/tags."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIST_LIMIT,
                },
            },
            "required": ["query"],
        }

    @property
    def toolset(self) -> str:
        return NOTEBOOK_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await _execute_notebook(
                "notebook.search",
                {
                    "query": kwargs.get("query"),
                    "limit": min(int(kwargs.get("limit", 20) or 20), MAX_LIST_LIMIT),
                },
            )
        except Exception as e:
            return f"notebook.search failed: {e}"
        entries = result.get("entries") or []
        if not entries:
            return f"No matches for {kwargs.get('query')!r}."
        return "\n\n".join(_format_entry(e) for e in entries)


# Self-register on import. The tool registry's discover() picks this up
# once we add it to the _TOOL_MODULES list in registry.py.
registry.register(NotebookWriteTool())
registry.register(NotebookReadTool())
registry.register(NotebookListTool())
registry.register(NotebookSearchTool())
