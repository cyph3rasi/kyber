"""Kyber Network — pairing and live link between Kyber instances.

Phase 1 scope: pair a spoke to a host over one-time code, then keep a
persistent WebSocket open for heartbeat and (in later phases) shared
notebook RPC and cross-machine tool invocation.

Trust model is simple: after pairing, every message is signed with an
HMAC-SHA256 secret exchanged during the handshake. Spokes always open
connections outbound so no inbound port is required on the spoke side.

Layout:

* :mod:`kyber.network.state`    — read/write ``~/.kyber/network.json``
* :mod:`kyber.network.protocol` — message envelopes + auth helpers
* :mod:`kyber.network.host`     — FastAPI routes + peer registry (runs on
                                  the gateway when ``config.network.role``
                                  is ``"host"``)
* :mod:`kyber.network.spoke`    — outbound WebSocket client with reconnect
                                  (runs on the gateway when role is ``"spoke"``)
"""

from kyber.network.state import (
    NetworkState,
    PairedPeer,
    get_or_create_identity,
    load_state,
    save_state,
)

__all__ = [
    "NetworkState",
    "PairedPeer",
    "get_or_create_identity",
    "load_state",
    "save_state",
]
