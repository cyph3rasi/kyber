"""Spoke-side of the Kyber network.

Runs inside the gateway process when ``config.network.role == "spoke"``.
Opens a persistent WebSocket to the configured host, sends a signed
``hello``, then pings a ``heartbeat`` every :data:`HEARTBEAT_INTERVAL_S`
seconds. Reconnects with backoff on any failure.

Outbound-only by design: spokes work behind NAT/firewall without any
port forwarding, because the connection is always initiated from the
spoke side.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import httpx
import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    WebSocketException,
)

from kyber.network.protocol import (
    Envelope,
    ProtocolError,
    decode_signed,
    encode_signed,
    make_envelope,
    make_rpc_request,
)
from kyber.network.state import (
    ROLE_HOST,
    ROLE_SPOKE,
    NetworkState,
    PairedPeer,
    load_state,
    save_state,
)

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 15.0
BACKOFF_MIN_S = 2.0
BACKOFF_MAX_S = 60.0
# How long ``call_rpc`` waits for the reconnect loop to restore the WS
# after a mid-call drop. Short enough to fail fast, long enough to cover
# a typical gateway restart.
FLAP_RECOVER_S = 8.0


async def pair_with_host(
    host_http_url: str,
    pairing_code: str,
    *,
    display_name: str | None = None,
    timeout: float = 20.0,
) -> NetworkState:
    """One-time pairing: POST to the host's ``/network/pair``.

    Persists the resulting secret + peer info into ``~/.kyber/network.json``
    and returns the updated state. Raises ``RuntimeError`` with a clean
    message on any failure so callers can print it directly.
    """
    state = load_state()
    payload = {
        "code": pairing_code.strip().upper(),
        "peer_id": state.peer_id,
        "name": (display_name or state.name).strip() or state.name,
    }

    url = host_http_url.rstrip("/") + "/network/pair"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
    except httpx.HTTPError as e:
        raise RuntimeError(f"could not reach host at {host_http_url}: {e}") from e

    if resp.status_code == 403:
        raise RuntimeError("pairing code was rejected — expired or already used")
    if resp.status_code == 409:
        raise RuntimeError("the target is not running in host mode")
    if resp.status_code != 200:
        snippet = (resp.text or "")[:300]
        raise RuntimeError(f"host returned HTTP {resp.status_code}: {snippet}")

    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError(f"host returned non-JSON response: {e}") from e

    host_peer_id = str(data.get("host_peer_id") or "")
    host_name = str(data.get("host_name") or "")
    secret = str(data.get("secret") or "")
    if not (host_peer_id and host_name and secret):
        raise RuntimeError("host returned an incomplete pairing response")

    now = time.time()
    # Flip BOTH the state role and persist it — the config.network.role is
    # updated separately by the caller (CLI flips it via save_config so
    # gateway restart picks it up).
    state.role = ROLE_SPOKE
    state.host_url = host_http_url.rstrip("/")
    state.name = payload["name"]  # update display name if user passed --as
    state.host_peer = PairedPeer(
        peer_id=host_peer_id,
        name=host_name,
        secret=secret,
        role=ROLE_HOST,
        added_at=now,
        last_seen=0.0,
    )
    save_state(state)
    return state


class SpokeClient:
    """Persistent WS client for a paired spoke.

    Owns the outbound WebSocket to the host plus the RPC correlation map.
    Callers can issue RPCs from any coroutine via :meth:`call_rpc` — the
    client serializes the write, records the pending future keyed by the
    envelope id, and resolves it when the matching ``rpc_response`` frame
    lands. ``call_rpc`` raises if the link isn't connected.

    Usage::

        client = get_spoke_client()
        await client.start()      # returns immediately; connects in background
        result = await client.call_rpc("notebook.write", {...}, timeout=10.0)
        await client.stop()
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_connected_at: float = 0.0
        self._last_error: str = ""

        # Active connection state. Populated inside _run_once while the
        # WebSocket is open; cleared on disconnect.
        self._send_lock = asyncio.Lock()
        self._current_ws = None  # type: ignore[assignment]
        self._current_secret: str = ""
        self._pending: dict[str, asyncio.Future] = {}
        self._connected = asyncio.Event()

    @property
    def status(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "connected": self._connected.is_set(),
            "last_connected_at": self._last_connected_at,
            "last_error": self._last_error,
        }

    # ── Public RPC surface ────────────────────────────────────────

    async def call_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 20.0,
        target: str | None = None,
    ) -> dict[str, Any]:
        """Issue an RPC to the host and return the result dict.

        Raises :class:`RuntimeError` if the link isn't up, the request
        times out, or the host returns a structured error.

        Flap tolerance: if the link drops mid-call (``"link lost"``), we
        wait up to ``FLAP_RECOVER_S`` for the reconnect loop to re-establish
        the WS, then retry the call once. Makes brief gateway restarts
        invisible to the agent.
        """
        last_err: Exception | None = None
        for attempt in (1, 2):
            if not self._connected.is_set() or self._current_ws is None:
                # Give the reconnect loop a chance before failing.
                try:
                    await asyncio.wait_for(
                        self._connected.wait(), timeout=FLAP_RECOVER_S
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError("no live link to host")

            env = make_rpc_request(method, params, target=target)
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[env.id] = fut
            try:
                await self._send_env(env)
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as e:
                raise RuntimeError(f"rpc {method} timed out after {timeout}s") from e
            except RuntimeError as e:
                # "link lost" comes from _run_once's cleanup when the WS
                # dies. Retry once after the reconnect loop picks it back up.
                last_err = e
                if "link lost" in str(e) and attempt == 1:
                    logger.info("network: link lost mid-rpc %s, retrying once", method)
                    continue
                raise
            finally:
                self._pending.pop(env.id, None)
        raise last_err or RuntimeError("rpc failed")

    async def _send_env(self, env: Envelope) -> None:
        ws = self._current_ws
        if ws is None:
            raise RuntimeError("no live link to host")
        async with self._send_lock:
            await ws.send(encode_signed(env, self._current_secret))

    async def send_notification(self, env: Envelope) -> None:
        """Fire-and-forget an envelope to the host (no response expected).

        Used for ``name_update`` and similar one-way events. Silently no-op
        if the link is down — those events re-sync at handshake on the next
        reconnect.
        """
        if not self._connected.is_set() or self._current_ws is None:
            return
        try:
            await self._send_env(env)
        except Exception:
            logger.debug("network: send_notification failed", exc_info=True)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_forever(), name="network-spoke")

    async def stop(self) -> None:
        self._stopping.set()
        t = self._task
        self._task = None
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _run_forever(self) -> None:
        backoff = BACKOFF_MIN_S
        while not self._stopping.is_set():
            state = load_state()
            if state.role != ROLE_SPOKE or state.host_peer is None or not state.host_url:
                # Not configured to run — park quietly. A later `kyber network
                # join` will update state; we'll pick it up on the next cycle.
                await self._sleep_or_stop(10.0)
                continue

            try:
                await self._run_once(state)
                # Clean close — reset backoff and loop.
                backoff = BACKOFF_MIN_S
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, ConnectionClosedError, ConnectionClosedOK) as e:
                self._last_error = f"disconnected: {e}"
                logger.info("network: spoke disconnected (%s); reconnecting in %.1fs", e, backoff)
            except WebSocketException as e:
                self._last_error = f"ws error: {e}"
                logger.info("network: ws error (%s); reconnecting in %.1fs", e, backoff)
            except Exception as e:  # pragma: no cover
                self._last_error = f"{type(e).__name__}: {e}"
                logger.exception("network: spoke loop failed")

            await self._sleep_or_stop(backoff)
            backoff = min(BACKOFF_MAX_S, backoff * 1.7)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run_once(self, state: NetworkState) -> None:
        ws_url = _http_to_ws(state.host_url) + "/ws/network"
        peer = state.host_peer
        assert peer is not None  # guarded above

        async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
            # Handshake.
            hello = make_envelope(
                "hello",
                {
                    "peer_id": state.peer_id,
                    "name": state.name,
                    "role": ROLE_SPOKE,
                },
            )
            await ws.send(encode_signed(hello, peer.secret))
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            try:
                env = decode_signed(raw, peer.secret)
            except ProtocolError as e:
                raise RuntimeError(f"host handshake invalid: {e}") from e
            if env.type != "hello_ack":
                raise RuntimeError(f"unexpected handshake reply: {env.type}")

            # Host may have renamed itself since our last connect — pick up
            # the fresh display name from the ack payload.
            ack_name = str((env.payload or {}).get("host_name") or "").strip()
            if ack_name:
                _update_host_name(ack_name[:40])

            # Handshake succeeded — expose the socket for RPC calls and
            # bump our local host_peer.last_seen immediately so the UI
            # doesn't show "offline" for the first 15s until a heartbeat
            # round-trips.
            self._current_ws = ws
            self._current_secret = peer.secret
            self._connected.set()
            self._last_connected_at = time.time()
            self._last_error = ""
            _touch_host_peer()
            logger.info("network: connected to host %s at %s", peer.name, state.host_url)

            try:
                # Split the loop into a heartbeat timer + a recv dispatcher
                # so RPC responses and heartbeats interleave cleanly.
                heartbeat = asyncio.create_task(
                    self._heartbeat_loop(), name="network-heartbeat"
                )
                recv = asyncio.create_task(self._recv_loop(ws), name="network-recv")
                try:
                    done, pending = await asyncio.wait(
                        {heartbeat, recv, asyncio.create_task(self._stopping.wait())},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await t
                    # Surface any exception from the first-completed task so
                    # _run_forever can log + back off.
                    for t in done:
                        exc = t.exception()
                        if exc is not None and not isinstance(exc, asyncio.CancelledError):
                            raise exc
                finally:
                    heartbeat.cancel()
                    recv.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                    with contextlib.suppress(asyncio.CancelledError):
                        await recv
            finally:
                self._current_ws = None
                self._current_secret = ""
                self._connected.clear()
                # Fail any in-flight RPCs so callers don't hang.
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(RuntimeError("link lost"))
                self._pending.clear()

    async def _heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            beat = make_envelope("heartbeat", {})
            await self._send_env(beat)
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=HEARTBEAT_INTERVAL_S
                )
            except asyncio.TimeoutError:
                continue

    async def _recv_loop(self, ws) -> None:
        while not self._stopping.is_set():
            raw = await ws.recv()
            try:
                env = decode_signed(raw, self._current_secret)
            except ProtocolError as e:
                raise RuntimeError(f"bad frame from host: {e}") from e
            if env.type == "heartbeat_ack":
                _touch_host_peer()
            elif env.type == "name_update":
                # Host renamed itself — update our local host_peer.name so
                # the UI shows the new value without waiting for a reconnect.
                payload = env.payload or {}
                new_name = str(payload.get("name") or "").strip()
                if new_name:
                    _update_host_name(new_name[:40])
            elif env.type == "rpc_request":
                # Host is invoking something on us (or forwarding from
                # another spoke). Dispatch locally and send rpc_response.
                from kyber.network.rpc import build_rpc_response

                state = load_state()
                host = state.host_peer
                response = await build_rpc_response(
                    env,
                    caller_peer_id=host.peer_id if host else "",
                    caller_name=host.name if host else "host",
                )
                await self._send_env(response)
            elif env.type == "rpc_response":
                fut = self._pending.get(env.id)
                if fut is None or fut.done():
                    logger.debug(
                        "network: rpc_response for unknown id %s", env.id
                    )
                    continue
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
            else:
                # Future frame types (e.g. tool_invoke from host → spoke).
                logger.debug("network: unhandled frame type %s", env.type)


def _http_to_ws(url: str) -> str:
    """``http://host:port`` → ``ws://host:port``; ``https://`` → ``wss://``."""
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    return url


def _touch_host_peer() -> None:
    state = load_state()
    if state.host_peer is not None:
        state.host_peer.last_seen = time.time()
        save_state(state)


def _update_host_name(new_name: str) -> None:
    state = load_state()
    if state.host_peer is not None and new_name and state.host_peer.name != new_name:
        state.host_peer.name = new_name
        save_state(state)


# Module-level singleton so the gateway lifecycle and CLI status command can
# share the same running client.
_SPOKE: SpokeClient | None = None


def get_spoke_client() -> SpokeClient:
    global _SPOKE
    if _SPOKE is None:
        _SPOKE = SpokeClient()
    return _SPOKE
