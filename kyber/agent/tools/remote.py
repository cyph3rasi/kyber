"""Remote tool execution across the Kyber network.

Agent tools that reach into another paired machine and run a tool there:

* :class:`ListPeersTool` — discover who you can invoke on
* :class:`ExecOnTool`    — run a shell command on a peer (same args as ``exec``)
* :class:`ReadFileOnTool` — read a file on a peer (same args as ``read_file``)
* :class:`ListDirOnTool` — list a directory on a peer (same args as ``list_dir``)
* :class:`RemoteInvokeTool` — escape hatch for any allow-listed tool

Routing is transparent to the LLM: it just names the target peer. If we're
the host, we directly send the RPC down that peer's WebSocket. If we're a
spoke targeting any other node (including sibling spokes), the request
goes to the host with ``target`` set, and the host relays. Either way the
execution node enforces its own ``network.exposed_tools`` allowlist —
that's where the security decision actually lives.

This module deliberately doesn't expose remote variants of destructive
tools like ``write_file``/``edit_file``. Add them yourself by calling the
generic ``remote_invoke`` if you know what you're doing.
"""

from __future__ import annotations

import logging
from typing import Any

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry

logger = logging.getLogger(__name__)

REMOTE_TOOLSET = "network"


# ── Peer resolution + RPC routing ────────────────────────────────────


async def _resolve_peer(peer_name: str) -> tuple[str, str]:
    """Find a peer by display name. Returns (peer_id, role).

    Looks in local state first (host's paired list or spoke's host peer).
    If we're a spoke and don't find it locally, asks the host over RPC —
    that gets us sibling spokes we haven't been told about.
    """
    from kyber.network.state import ROLE_HOST, ROLE_SPOKE, load_state

    name = (peer_name or "").strip()
    if not name:
        raise ValueError("peer_name is required")

    state = load_state()

    candidates: list[tuple[str, str]] = []  # (peer_id, role)
    if state.role == ROLE_HOST:
        for p in state.paired_peers:
            if p.name == name:
                candidates.append((p.peer_id, p.role))
    elif state.role == ROLE_SPOKE and state.host_peer is not None:
        if state.host_peer.name == name:
            candidates.append((state.host_peer.peer_id, state.host_peer.role))

    if not candidates and state.role == ROLE_SPOKE:
        # Ask the host for the full peer list (includes sibling spokes).
        from kyber.network.spoke import get_spoke_client

        client = get_spoke_client()
        if client.status.get("connected"):
            try:
                peers = await client.call_rpc(
                    "network.list_peers", {}, timeout=5.0
                )
                for p in (peers or {}).get("peers", []):
                    if p.get("name") == name and not p.get("self"):
                        candidates.append((str(p["peer_id"]), str(p.get("role") or "")))
            except Exception:
                pass

    if not candidates:
        raise RuntimeError(
            f"no paired peer named {name!r}. "
            "Use list_network_peers to see who's available."
        )
    if len(candidates) > 1:
        logger.info("multiple peers match %r; using first", name)
    return candidates[0]


async def _invoke_remote_tool(
    peer_name: str, tool_name: str, params: dict[str, Any]
) -> str:
    """Run a tool on a remote peer by name. Returns the tool's output string.

    Transparently routes through the local node's role — hosts send direct,
    spokes either call their host (if the target IS the host) or send to
    the host with a ``target`` field so it relays to a sibling.
    """
    from kyber.network.state import ROLE_HOST, ROLE_SPOKE, load_state

    target_id, _ = await _resolve_peer(peer_name)
    state = load_state()

    invoke_params = {"tool_name": tool_name, "params": params}

    if state.role == ROLE_HOST:
        from kyber.network.host import get_registry

        result = await get_registry().call_peer(
            target_id, "tool.invoke", invoke_params, timeout=60.0
        )
    elif state.role == ROLE_SPOKE:
        from kyber.network.spoke import get_spoke_client

        client = get_spoke_client()
        if not client.status.get("connected"):
            raise RuntimeError("not connected to the host; check `kyber network status`")
        # Target may be the host itself (no relay) or a sibling (host relays).
        # `target` must go on the envelope's top-level payload, NOT inside
        # params — the host's relay check reads `payload.get("target")`.
        if state.host_peer is not None and target_id == state.host_peer.peer_id:
            result = await client.call_rpc("tool.invoke", invoke_params, timeout=60.0)
        else:
            result = await client.call_rpc(
                "tool.invoke",
                invoke_params,
                timeout=60.0,
                target=target_id,
            )
    else:
        raise RuntimeError(
            "this Kyber isn't paired with a network — run `kyber network pair` / `join`"
        )

    if not isinstance(result, dict):
        return str(result)
    if "output" in result:
        return str(result.get("output"))
    return str(result)


# ── Tool definitions ─────────────────────────────────────────────────


class ListPeersTool(Tool):
    @property
    def name(self) -> str:
        return "list_network_peers"

    @property
    def description(self) -> str:
        return (
            "List paired Kyber peers. Call before exec_on/read_file_on/"
            "list_dir_on to find the target's display name."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def toolset(self) -> str:
        return REMOTE_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        from kyber.network.rpc import _all_peers_known
        from kyber.network.state import ROLE_SPOKE, load_state

        state = load_state()
        peers = _all_peers_known()

        # Spokes only know themselves + host locally; ask the host for siblings.
        if state.role == ROLE_SPOKE:
            from kyber.network.spoke import get_spoke_client

            client = get_spoke_client()
            if client.status.get("connected"):
                try:
                    remote = await client.call_rpc("network.list_peers", {}, timeout=5.0)
                    peers = (remote or {}).get("peers") or peers
                except Exception:
                    pass

        if not peers:
            return "No peers — this machine isn't on a Kyber network yet."
        lines = ["Known peers:"]
        for p in peers:
            marker = " (this machine)" if p.get("self") else ""
            lines.append(
                f"- {p.get('name', '?')}{marker} · role={p.get('role', '?')}"
            )
        return "\n".join(lines)


class ExecOnTool(Tool):
    @property
    def name(self) -> str:
        return "exec_on"

    @property
    def description(self) -> str:
        return (
            "Run a shell command on a paired machine. Use when the user asks "
            "you to do something on another host — this is the real action, "
            "not a notebook write. Target needs `exec` in its exposedTools."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "peer_name": {"type": "string", "minLength": 1},
                "command": {"type": "string", "minLength": 1},
            },
            "required": ["peer_name", "command"],
        }

    @property
    def toolset(self) -> str:
        return REMOTE_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        peer = kwargs.get("peer_name") or ""
        command = kwargs.get("command") or ""
        try:
            return await _invoke_remote_tool(peer, "exec", {"command": command})
        except Exception as e:
            return f"exec_on({peer!r}) failed: {e}"


class ReadFileOnTool(Tool):
    @property
    def name(self) -> str:
        return "read_file_on"

    @property
    def description(self) -> str:
        return "Read a file from a paired machine (target needs `read_file` exposed)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "peer_name": {"type": "string", "minLength": 1},
                "path": {"type": "string", "minLength": 1},
            },
            "required": ["peer_name", "path"],
        }

    @property
    def toolset(self) -> str:
        return REMOTE_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        peer = kwargs.get("peer_name") or ""
        path = kwargs.get("path") or ""
        try:
            return await _invoke_remote_tool(peer, "read_file", {"path": path})
        except Exception as e:
            return f"read_file_on({peer!r}) failed: {e}"


class ListDirOnTool(Tool):
    @property
    def name(self) -> str:
        return "list_dir_on"

    @property
    def description(self) -> str:
        return "List a directory on a paired machine (target needs `list_dir` exposed)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "peer_name": {"type": "string", "minLength": 1},
                "path": {"type": "string", "minLength": 1},
            },
            "required": ["peer_name", "path"],
        }

    @property
    def toolset(self) -> str:
        return REMOTE_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        peer = kwargs.get("peer_name") or ""
        path = kwargs.get("path") or ""
        try:
            return await _invoke_remote_tool(peer, "list_dir", {"path": path})
        except Exception as e:
            return f"list_dir_on({peer!r}) failed: {e}"


class WriteFileOnTool(Tool):
    @property
    def name(self) -> str:
        return "write_file_on"

    @property
    def description(self) -> str:
        return "Create/overwrite a file on a paired machine (needs `write_file` exposed)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "peer_name": {"type": "string", "minLength": 1},
                "path": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
            },
            "required": ["peer_name", "path", "content"],
        }

    @property
    def toolset(self) -> str:
        return REMOTE_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        peer = kwargs.get("peer_name") or ""
        path = kwargs.get("path") or ""
        content = kwargs.get("content", "")
        try:
            return await _invoke_remote_tool(
                peer, "write_file", {"path": path, "content": content}
            )
        except Exception as e:
            return f"write_file_on({peer!r}) failed: {e}"


class EditFileOnTool(Tool):
    @property
    def name(self) -> str:
        return "edit_file_on"

    @property
    def description(self) -> str:
        return "In-place edit a file on a paired machine (needs `edit_file` exposed)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "peer_name": {"type": "string", "minLength": 1},
                "path": {"type": "string", "minLength": 1},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["peer_name", "path", "old_string", "new_string"],
        }

    @property
    def toolset(self) -> str:
        return REMOTE_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        peer = kwargs.get("peer_name") or ""
        path = kwargs.get("path") or ""
        old = kwargs.get("old_string", "")
        new = kwargs.get("new_string", "")
        try:
            return await _invoke_remote_tool(
                peer,
                "edit_file",
                {"path": path, "old_string": old, "new_string": new},
            )
        except Exception as e:
            return f"edit_file_on({peer!r}) failed: {e}"


class RemoteInvokeTool(Tool):
    @property
    def name(self) -> str:
        return "remote_invoke"

    @property
    def description(self) -> str:
        return "Escape hatch: invoke any exposed tool on a peer. Prefer exec_on / read_file_on / etc."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "peer_name": {"type": "string", "minLength": 1},
                "tool_name": {"type": "string", "minLength": 1},
                "params": {"type": "object"},
            },
            "required": ["peer_name", "tool_name"],
        }

    @property
    def toolset(self) -> str:
        return REMOTE_TOOLSET

    async def execute(self, **kwargs: Any) -> str:
        peer = kwargs.get("peer_name") or ""
        tool_name = kwargs.get("tool_name") or ""
        params = kwargs.get("params") or {}
        if not isinstance(params, dict):
            return "remote_invoke: params must be an object"
        try:
            return await _invoke_remote_tool(peer, tool_name, params)
        except Exception as e:
            return f"remote_invoke({peer!r}, {tool_name!r}) failed: {e}"


registry.register(ListPeersTool())
registry.register(ExecOnTool())
registry.register(ReadFileOnTool())
registry.register(ListDirOnTool())
registry.register(WriteFileOnTool())
registry.register(EditFileOnTool())
registry.register(RemoteInvokeTool())
