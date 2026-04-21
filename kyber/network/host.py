"""Host-side of the Kyber network.

Runs inside the gateway process when ``config.network.role == "host"``.
Mounts three surfaces:

* ``POST /network/pair``      — consume a one-time pairing code and return a
                                shared secret. Called once per spoke.
* ``WS   /ws/network``        — persistent link with a paired spoke.
* ``GET  /network/peers``     — status for the dashboard.

Pairing codes live only in memory (plus a hashed record on disk for the
in-flight set), expire after :data:`PAIRING_CODE_TTL_S`, and are burned
after a single successful use. Past that handshake, every frame is HMAC
signed — the code is never used again.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from kyber.network.protocol import (
    Envelope,
    ProtocolError,
    decode_signed,
    encode_signed,
    make_envelope,
    make_rpc_response,
)
from kyber.network.state import (
    ROLE_HOST,
    ROLE_SPOKE,
    NetworkState,
    PairedPeer,
    load_state,
    new_secret,
    save_state,
)

logger = logging.getLogger(__name__)

PAIRING_CODE_TTL_S = 300.0  # 5 minutes
HEARTBEAT_MARK_STALE_S = 45.0  # declare a peer "offline" in the UI after this


@dataclass
class _PendingCode:
    code: str
    created_at: float
    # Host-side label to attach if the spoke doesn't send one. Otherwise
    # we use whatever the spoke announced as its display name.
    expected_name: str | None = None


class HostRegistry:
    """In-memory registry of pending pairings + live WS connections.

    The persistent peer list lives in ``NetworkState``; this class just
    tracks which of those peers currently has an open WebSocket. It also
    owns the correlation map for RPCs the host INITIATES toward a spoke,
    and for RPCs the host RELAYS between two spokes — both flows need to
    match ``rpc_response`` frames back to a pending future.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingCode] = {}
        self._live: dict[str, WebSocket] = {}
        # Correlation map for host-initiated and relayed RPCs.
        #   request_id -> Future that resolves with the result dict.
        self._rpc_pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    # ── Pairing codes ────────────────────────────────────────────

    def new_pairing_code(self, expected_name: str | None = None) -> str:
        """Generate a human-typable one-time code; store it for later redemption."""
        self._prune_expired()
        code = _format_code()
        self._pending[code] = _PendingCode(
            code=code,
            created_at=time.time(),
            expected_name=(expected_name or "").strip() or None,
        )
        return code

    def consume_pairing_code(self, code: str) -> _PendingCode | None:
        """Atomically take and remove a pending code. Returns None if missing/expired."""
        self._prune_expired()
        pending = self._pending.pop(code, None)
        if pending is None:
            return None
        return pending

    def _prune_expired(self) -> None:
        now = time.time()
        for code, pending in list(self._pending.items()):
            if now - pending.created_at > PAIRING_CODE_TTL_S:
                self._pending.pop(code, None)

    # ── Live WebSocket tracking ──────────────────────────────────

    async def attach(self, peer_id: str, ws: WebSocket) -> None:
        async with self._lock:
            prev = self._live.get(peer_id)
            self._live[peer_id] = ws
        if prev is not None:
            # Best-effort close of the replaced connection.
            try:
                await prev.close(code=4002, reason="superseded")
            except Exception:
                pass

    async def call_peer(
        self,
        peer_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        """Issue an RPC to a specific connected spoke and return the result.

        Raises ``RuntimeError`` if the peer isn't connected or times out,
        and re-raises structured ``rpc_response`` errors as ``RuntimeError``.
        """
        from kyber.network.protocol import make_rpc_request

        async with self._lock:
            ws = self._live.get(peer_id)
        state = load_state()
        peer = state.get_peer(peer_id)
        if ws is None or peer is None or ws.client_state != WebSocketState.CONNECTED:
            raise RuntimeError(f"peer {peer_id[:12]}… is not connected")

        env = make_rpc_request(method, params or {})
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._rpc_pending[env.id] = fut
        try:
            await ws.send_text(encode_signed(env, peer.secret))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"rpc {method} to {peer.name} timed out after {timeout}s"
            ) from e
        finally:
            self._rpc_pending.pop(env.id, None)

    def resolve_rpc_response(
        self,
        env: Envelope,
    ) -> bool:
        """Resolve a pending host-initiated or relayed RPC from a response env.

        Returns True if the envelope matched a known pending id. Called from
        the WS receive loop for each ``rpc_response`` frame.
        """
        fut = self._rpc_pending.get(env.id)
        if fut is None or fut.done():
            return False
        payload = env.payload or {}
        err = payload.get("error")
        if err:
            fut.set_exception(
                RuntimeError(
                    f"{err.get('code', 'error')}: {err.get('message', 'rpc error')}"
                )
            )
        else:
            fut.set_result(payload.get("result"))
        return True

    async def broadcast(self, env: Envelope) -> None:
        """Send an envelope to every currently-live spoke, HMAC-signed per-peer.

        Each spoke has its own shared secret, so we can't reuse a single
        encoded frame — we re-sign per connection. Dead sockets fall out
        of the live map; the spoke's reconnect loop will restore them.
        """
        state = load_state()
        dead: list[str] = []
        async with self._lock:
            items = list(self._live.items())
        for peer_id, ws in items:
            peer = state.get_peer(peer_id)
            if peer is None or ws.client_state != WebSocketState.CONNECTED:
                dead.append(peer_id)
                continue
            try:
                await ws.send_text(encode_signed(env, peer.secret))
            except Exception:
                dead.append(peer_id)
        if dead:
            async with self._lock:
                for pid in dead:
                    self._live.pop(pid, None)

    async def detach(self, peer_id: str, ws: WebSocket) -> None:
        async with self._lock:
            if self._live.get(peer_id) is ws:
                self._live.pop(peer_id, None)

    def is_live(self, peer_id: str) -> bool:
        ws = self._live.get(peer_id)
        if ws is None:
            return False
        return ws.client_state == WebSocketState.CONNECTED


_REGISTRY: HostRegistry | None = None


def get_registry() -> HostRegistry:
    """Module-level singleton so the CLI and gateway share the same registry."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = HostRegistry()
    return _REGISTRY


# ── Public helpers for CLI and dashboard ─────────────────────────────


def host_generate_pairing_code(expected_name: str | None = None) -> str:
    """Public helper for ``kyber network pair`` — just calls through."""
    return get_registry().new_pairing_code(expected_name)


def host_list_peers_with_status() -> list[dict[str, Any]]:
    """Return a dashboard-ready list of paired peers with live/offline state."""
    state = load_state()
    if state.role != ROLE_HOST:
        return []
    reg = get_registry()
    now = time.time()
    out: list[dict[str, Any]] = []
    for p in state.paired_peers:
        live = reg.is_live(p.peer_id)
        stale = (now - p.last_seen) > HEARTBEAT_MARK_STALE_S if p.last_seen else True
        out.append(
            {
                "peer_id": p.peer_id,
                "name": p.name,
                "role": p.role,
                "added_at": p.added_at,
                "last_seen": p.last_seen,
                "connected": live and not stale,
            }
        )
    return out


def host_unpair(peer_id: str) -> bool:
    """Remove a peer from persistent state. Live WS is closed by the caller."""
    state = load_state()
    before = len(state.paired_peers)
    state.paired_peers = [p for p in state.paired_peers if p.peer_id != peer_id]
    if len(state.paired_peers) == before:
        return False
    save_state(state)
    return True


# ── FastAPI mount ────────────────────────────────────────────────────


def _bind_host_routes(app: FastAPI, require=None) -> None:
    """Attach the host's network endpoints to an existing FastAPI app.

    ``require`` is an optional FastAPI dependency used to gate admin-only
    endpoints (``/network/pair-code``, ``/network/peers/unpair``). The
    pairing + WebSocket endpoints themselves authenticate via the pairing
    code or HMAC signatures and don't need another layer.
    """
    registry = get_registry()

    @app.post("/network/pair")
    async def pair_endpoint(body: dict[str, Any]):
        code = str(body.get("code") or "").strip().upper()
        spoke_peer_id = str(body.get("peer_id") or "").strip()
        spoke_name = str(body.get("name") or "").strip()
        if not code or not spoke_peer_id or not spoke_name:
            raise HTTPException(status_code=400, detail="code, peer_id, name are required")

        pending = registry.consume_pairing_code(code)
        if pending is None:
            raise HTTPException(status_code=403, detail="invalid or expired pairing code")

        state = load_state()
        if state.role != ROLE_HOST:
            raise HTTPException(status_code=409, detail="this instance is not in host mode")

        # Replace any existing peer with the same id — spokes re-pair after factory reset.
        state.paired_peers = [p for p in state.paired_peers if p.peer_id != spoke_peer_id]
        secret = new_secret()
        now = time.time()
        state.paired_peers.append(
            PairedPeer(
                peer_id=spoke_peer_id,
                name=spoke_name[:40] or "spoke",
                secret=secret,
                role=ROLE_SPOKE,
                added_at=now,
                last_seen=0.0,
            )
        )
        save_state(state)

        return {
            "host_peer_id": state.peer_id,
            "host_name": state.name,
            "secret": secret,
        }

    from fastapi import Depends

    auth_deps = [Depends(require)] if require is not None else []

    @app.post("/network/pair-code", dependencies=auth_deps)
    async def pair_code_endpoint(body: dict[str, Any] | None = None):
        """Admin: generate a one-time pairing code. Used by CLI + dashboard."""
        state = load_state()
        if state.role != ROLE_HOST:
            raise HTTPException(status_code=409, detail="not running in host mode")
        payload = body or {}
        label = str(payload.get("name") or "").strip() or None
        code = registry.new_pairing_code(expected_name=label)
        return {"code": code, "expires_in": int(PAIRING_CODE_TTL_S)}

    @app.post("/network/self", dependencies=auth_deps)
    async def set_self_endpoint(body: dict[str, Any]):
        """Admin: update this machine's display name.

        Only the ``name`` field is editable — ``peer_id`` is permanent so
        paired peers keep working across renames. After saving we push a
        ``name_update`` frame over every active network link so peers
        tracking this machine see the new name without waiting for a
        reconnect.
        """
        new_name = str(body.get("name") or "").strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="name is required")
        if len(new_name) > 40:
            raise HTTPException(status_code=400, detail="name must be 40 chars or fewer")

        state = load_state()
        state.name = new_name
        save_state(state)

        # Propagate. Direction depends on role:
        #   host  → every connected spoke (via registry.broadcast)
        #   spoke → the host (via the SpokeClient)
        env = make_envelope("name_update", {"peer_id": state.peer_id, "name": new_name})
        if state.role == ROLE_HOST:
            try:
                await registry.broadcast(env)
            except Exception:
                logger.exception("network: failed to broadcast name_update")
        elif state.role == ROLE_SPOKE:
            try:
                from kyber.network.spoke import get_spoke_client

                await get_spoke_client().send_notification(env)
            except Exception:
                logger.exception("network: failed to send name_update to host")

        return {"peer_id": state.peer_id, "name": state.name}

    @app.post("/network/role", dependencies=auth_deps)
    async def set_role_endpoint(body: dict[str, Any]):
        """Admin: switch this instance between standalone / host / spoke.

        Writes to both the state file (so pair-code/pair endpoints see it
        immediately) and ``config.network.role`` (so the gateway picks the
        right role at next restart). Callers must restart the gateway to
        start/stop the spoke WebSocket loop.
        """
        from kyber.config.loader import load_config, save_config
        from kyber.network.state import (
            ROLE_HOST as _H,
            ROLE_SPOKE as _S,
            ROLE_STANDALONE as _NONE,
            save_state,
        )

        new_role = str(body.get("role") or "").strip().lower()
        if new_role not in (_NONE, _H, _S):
            raise HTTPException(status_code=400, detail="role must be standalone|host|spoke")

        # Spokes are set up via the /network/join flow, not this endpoint —
        # blindly flipping to spoke here would leave host_peer unset.
        if new_role == _S:
            raise HTTPException(
                status_code=400,
                detail="to become a spoke, POST /network/join with host_url + code",
            )

        state = load_state()
        state.role = new_role
        if new_role != _S:
            # Dropping out of spoke mode clears the host linkage.
            state.host_url = ""
            state.host_peer = None
        save_state(state)

        cfg = load_config()
        cfg.network.role = new_role
        save_config(cfg)

        return {
            "role": new_role,
            "needs_restart": True,
            "self": {"peer_id": state.peer_id, "name": state.name},
        }

    @app.post("/network/join", dependencies=auth_deps)
    async def join_endpoint(body: dict[str, Any]):
        """Admin: pair this instance as a spoke to a remote host.

        Same flow as ``kyber network join`` but callable from the UI so
        users don't have to drop to a terminal.
        """
        from kyber.config.loader import load_config, save_config
        from kyber.network.spoke import pair_with_host

        host_url = str(body.get("host_url") or "").strip()
        code = str(body.get("code") or "").strip()
        display_name = str(body.get("name") or "").strip() or None
        if not host_url or not code:
            raise HTTPException(status_code=400, detail="host_url and code are required")

        try:
            state = await pair_with_host(host_url, code, display_name=display_name)
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        cfg = load_config()
        cfg.network.role = "spoke"
        save_config(cfg)

        return {
            "role": "spoke",
            "needs_restart": True,
            "self": {"peer_id": state.peer_id, "name": state.name},
            "host": {
                "peer_id": state.host_peer.peer_id if state.host_peer else "",
                "name": state.host_peer.name if state.host_peer else "",
                "url": state.host_url,
            },
        }

    @app.post("/network/peers/unpair", dependencies=auth_deps)
    async def peers_unpair_endpoint(body: dict[str, Any]):
        """Admin: remove a paired peer by id. Closes live WS if any."""
        peer_id = str(body.get("peer_id") or "").strip()
        if not peer_id:
            raise HTTPException(status_code=400, detail="peer_id is required")
        removed = host_unpair(peer_id)
        return {"ok": removed}

    @app.get("/network/peers")
    async def peers_endpoint():
        state = load_state()
        if state.role == ROLE_HOST:
            return {
                "role": "host",
                "self": {"peer_id": state.peer_id, "name": state.name},
                "peers": host_list_peers_with_status(),
            }
        if state.role == ROLE_SPOKE and state.host_peer is not None:
            return {
                "role": "spoke",
                "self": {"peer_id": state.peer_id, "name": state.name},
                "host": {
                    "peer_id": state.host_peer.peer_id,
                    "name": state.host_peer.name,
                    "url": state.host_url,
                    "last_seen": state.host_peer.last_seen,
                },
            }
        return {
            "role": "standalone",
            "self": {"peer_id": state.peer_id, "name": state.name},
        }

    # ── Notebook HTTP endpoints (admin-auth'd) ──
    # These call the same routing logic agents use (local on host, RPC on
    # spoke), so the dashboard's Notebook tab works from either side of the
    # link. Scoped under /network/notebook/* to avoid name clashes with any
    # future per-project notebooks.

    @app.post("/network/notebook/list", dependencies=auth_deps)
    async def notebook_list_endpoint(body: dict[str, Any] | None = None):
        from kyber.agent.tools.notebook import _execute_notebook

        params = body or {}
        try:
            return await _execute_notebook("notebook.list", params)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/network/notebook/search", dependencies=auth_deps)
    async def notebook_search_endpoint(body: dict[str, Any]):
        from kyber.agent.tools.notebook import _execute_notebook

        try:
            return await _execute_notebook("notebook.search", body)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/network/notebook/read", dependencies=auth_deps)
    async def notebook_read_endpoint(body: dict[str, Any]):
        from kyber.agent.tools.notebook import _execute_notebook

        try:
            return await _execute_notebook("notebook.read", body)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/network/notebook/write", dependencies=auth_deps)
    async def notebook_write_endpoint(body: dict[str, Any]):
        from kyber.agent.tools.notebook import _execute_notebook

        try:
            return await _execute_notebook("notebook.write", body)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/network/notebook/delete", dependencies=auth_deps)
    async def notebook_delete_endpoint(body: dict[str, Any]):
        # Deleting is host-only for now — spokes can call it via RPC too but
        # we haven't exposed the 'delete' method over the network wire. Keep
        # it simple: dashboard's delete buttons only work when hosting.
        from kyber.network import notebook as nb
        from kyber.network.state import ROLE_HOST, load_state

        state = load_state()
        if state.role != ROLE_HOST:
            raise HTTPException(
                status_code=409, detail="notebook deletion requires host mode"
            )
        entry_id = int(body.get("id") or 0)
        if entry_id <= 0:
            raise HTTPException(status_code=400, detail="id is required")
        ok = nb.delete_entry(entry_id)
        return {"ok": ok}

    @app.websocket("/ws/network")
    async def network_ws(ws: WebSocket):
        await ws.accept()
        peer: PairedPeer | None = None
        try:
            # Handshake: first frame must be a signed "hello" from a paired peer.
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
            except asyncio.TimeoutError:
                await ws.close(code=4008, reason="handshake timeout")
                return

            # We don't know which peer is on the other end yet, so we peek at
            # the envelope claim and then verify with that peer's secret.
            claim = _peek_hello_peer_id(raw)
            if not claim:
                await ws.close(code=4000, reason="bad handshake")
                return
            state = load_state()
            peer = state.get_peer(claim)
            if peer is None:
                await ws.close(code=4001, reason="unknown peer")
                return

            try:
                env = decode_signed(raw, peer.secret)
            except ProtocolError as e:
                logger.info("network: rejecting peer %s handshake: %s", claim, e)
                await ws.close(code=4003, reason="bad signature")
                return

            if env.type != "hello":
                await ws.close(code=4000, reason="expected hello")
                return

            # Handshake accepted — refresh the peer's display name from the
            # claim in the hello payload (spoke might have renamed itself
            # since we paired), then attach and ack.
            claimed_name = str((env.payload or {}).get("name") or "").strip()
            if claimed_name and claimed_name != peer.name:
                _update_peer_name(peer.peer_id, claimed_name[:40])
                peer.name = claimed_name[:40]
            await registry.attach(peer.peer_id, ws)
            ack = make_envelope(
                "hello_ack",
                {"host_peer_id": state.peer_id, "host_name": state.name},
            )
            await ws.send_text(encode_signed(ack, peer.secret))
            _touch_peer(peer.peer_id)

            while True:
                raw = await ws.receive_text()
                try:
                    env = decode_signed(raw, peer.secret)
                except ProtocolError as e:
                    logger.info("network: peer %s sent bad frame: %s", peer.peer_id, e)
                    await ws.close(code=4003, reason="bad signature")
                    return

                if env.type == "heartbeat":
                    _touch_peer(peer.peer_id)
                    ack = make_envelope("heartbeat_ack", {})
                    await ws.send_text(encode_signed(ack, peer.secret))
                elif env.type == "rpc_request":
                    _touch_peer(peer.peer_id)

                    async def _relay(target: str, method: str, params, caller_id, caller_name):
                        """Forward an RPC from one spoke to another via this host.

                        We enrich params with caller info so the target node
                        can log/audit who triggered the call, and swap in a
                        fresh correlation id managed by our call_peer.
                        """
                        enriched = dict(params or {})
                        enriched["_from_peer_id"] = caller_id
                        enriched["_from_peer_name"] = caller_name
                        return await registry.call_peer(target, method, enriched)

                    from kyber.network.rpc import build_rpc_response

                    response = await build_rpc_response(
                        env,
                        caller_peer_id=peer.peer_id,
                        caller_name=peer.name,
                        relay_fn=_relay,
                    )
                    await ws.send_text(encode_signed(response, peer.secret))
                elif env.type == "rpc_response":
                    # Correlated with a host-initiated OR relayed RPC. If no
                    # pending future matches, this is stale — drop it.
                    registry.resolve_rpc_response(env)
                elif env.type == "name_update":
                    # Spoke renamed itself — trust the claim but stamp
                    # peer_id match (from handshake) so a spoke can't
                    # rename someone else.
                    payload = env.payload or {}
                    new_name = str(payload.get("name") or "").strip()
                    if new_name:
                        _update_peer_name(peer.peer_id, new_name[:40])
                        peer.name = new_name[:40]
                else:
                    # Unknown message type — ignore for forward compat.
                    logger.debug(
                        "network: peer %s sent unhandled type %s", peer.peer_id, env.type
                    )

        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("network: ws handler failed")
        finally:
            if peer is not None:
                await registry.detach(peer.peer_id, ws)


def mount_host_routes(app: FastAPI, require=None) -> None:
    """Gateway-side entry point. Safe to call whenever a gateway starts.

    Pass ``require`` (a FastAPI dependency callable) to gate admin-only
    endpoints behind auth — typically the gateway's bearer-token check.
    """
    _bind_host_routes(app, require=require)


# ── Internals ────────────────────────────────────────────────────────


def _format_code() -> str:
    """Six-group hex (``AB12-CD34``). Short enough to type, long enough to matter."""
    raw = secrets.token_hex(4).upper()  # 8 chars
    return f"{raw[:4]}-{raw[4:]}"


def _peek_hello_peer_id(raw: str) -> str | None:
    """Peek the claimed peer_id out of a hello payload without verifying the signature."""
    import json as _json

    try:
        data = _json.loads(raw)
        payload = data.get("payload") or {}
        pid = payload.get("peer_id")
        return str(pid) if pid else None
    except (ValueError, AttributeError):
        return None


def _handle_notebook_rpc(method: str, params: dict[str, Any], peer: PairedPeer) -> Any:
    """Route notebook.* RPC calls to :mod:`kyber.network.notebook`."""
    from kyber.network import notebook as nb

    name = method.split(".", 1)[1]
    if name == "write":
        entry = nb.write(
            key=str(params.get("key") or ""),
            value=params.get("value", ""),
            author_peer_id=peer.peer_id,
            author_name=peer.name,
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
    if name == "delete":
        ok = nb.delete_entry(int(params.get("id") or 0))
        return {"ok": ok}
    if name == "stats":
        return nb.stats()
    raise ValueError(f"unknown notebook method: {name}")


def _touch_peer(peer_id: str) -> None:
    """Stamp ``last_seen = now`` on a peer record and persist."""
    state = load_state()
    now = time.time()
    updated = False
    for p in state.paired_peers:
        if p.peer_id == peer_id:
            p.last_seen = now
            updated = True
            break
    if state.host_peer and state.host_peer.peer_id == peer_id:
        state.host_peer.last_seen = now
        updated = True
    if updated:
        save_state(state)


def _update_peer_name(peer_id: str, new_name: str) -> None:
    """Refresh a paired peer's display name from a claim it just sent us."""
    if not new_name:
        return
    state = load_state()
    changed = False
    for p in state.paired_peers:
        if p.peer_id == peer_id and p.name != new_name:
            p.name = new_name
            changed = True
            break
    if changed:
        save_state(state)
