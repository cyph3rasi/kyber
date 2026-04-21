"""Persistent network state for this Kyber instance.

Stored at ``~/.kyber/network.json`` (mode 0600). Holds:

* A stable per-machine identity (``peer_id`` + ``name``) — generated the
  first time anything in the network module runs.
* Role: ``"standalone"`` (default), ``"host"``, or ``"spoke"``.
* For a host: the list of currently paired peers and their shared secrets.
* For a spoke: which host we're paired with and our shared secret.

This file contains secrets, so every write goes through :func:`save_state`,
which writes to a tmp file, chmods it 0600, and atomically renames. Secrets
are HMAC keys used to sign every WebSocket frame between paired peers.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

NETWORK_STATE_PATH = Path.home() / ".kyber" / "network.json"

# Roles the user can set. "standalone" is the default; the network subsystem
# no-ops unless one of the other two is selected.
ROLE_STANDALONE = "standalone"
ROLE_HOST = "host"
ROLE_SPOKE = "spoke"
VALID_ROLES = {ROLE_STANDALONE, ROLE_HOST, ROLE_SPOKE}


@dataclass
class PairedPeer:
    """A machine we've paired with.

    On a host, this stores every spoke that's been paired to us. On a
    spoke, this usually has exactly one entry — our host.
    """

    peer_id: str
    name: str
    secret: str  # 32 bytes hex; HMAC key for signing WS frames
    role: str  # the other side's role, for sanity checks
    added_at: float = 0.0  # time.time() when pairing completed
    last_seen: float = 0.0  # server-side heartbeat timestamp


@dataclass
class NetworkState:
    """Everything persisted to ``~/.kyber/network.json``."""

    # This machine's stable identity.
    peer_id: str
    name: str

    # Role: "standalone" | "host" | "spoke"
    role: str = ROLE_STANDALONE

    # Spoke-only: URL of the host and the peer record for that host.
    host_url: str = ""
    host_peer: PairedPeer | None = None

    # Host-only: peers we've paired.
    paired_peers: list[PairedPeer] = field(default_factory=list)

    def get_peer(self, peer_id: str) -> PairedPeer | None:
        """Find a paired peer by id (host-side lookup)."""
        for p in self.paired_peers:
            if p.peer_id == peer_id:
                return p
        if self.host_peer and self.host_peer.peer_id == peer_id:
            return self.host_peer
        return None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # asdict turns PairedPeer and list[PairedPeer] into dicts; nothing else needed.
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NetworkState":
        peers_raw = data.get("paired_peers") or []
        paired = [PairedPeer(**_coerce_peer(p)) for p in peers_raw if isinstance(p, dict)]
        host_peer_raw = data.get("host_peer")
        host_peer = (
            PairedPeer(**_coerce_peer(host_peer_raw))
            if isinstance(host_peer_raw, dict)
            else None
        )
        return cls(
            peer_id=str(data.get("peer_id") or ""),
            name=str(data.get("name") or ""),
            role=str(data.get("role") or ROLE_STANDALONE),
            host_url=str(data.get("host_url") or ""),
            host_peer=host_peer,
            paired_peers=paired,
        )


def _coerce_peer(d: dict[str, Any]) -> dict[str, Any]:
    """Accept both snake_case and camelCase, fill in defaults."""
    return {
        "peer_id": str(d.get("peer_id") or d.get("peerId") or ""),
        "name": str(d.get("name") or ""),
        "secret": str(d.get("secret") or ""),
        "role": str(d.get("role") or ""),
        "added_at": float(d.get("added_at") or d.get("addedAt") or 0.0),
        "last_seen": float(d.get("last_seen") or d.get("lastSeen") or 0.0),
    }


def _default_name() -> str:
    """Default display name — the machine's hostname, trimmed to something nice."""
    import platform

    raw = platform.node() or "kyber"
    # Strip ".local" on macOS and keep it short.
    name = raw.split(".")[0] or "kyber"
    return name[:40] or "kyber"


def load_state(path: Path | None = None) -> NetworkState:
    """Load the network state file, returning a default state if it doesn't exist."""
    p = path or NETWORK_STATE_PATH
    if not p.is_file():
        return NetworkState(peer_id=str(uuid.uuid4()), name=_default_name())
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Corrupt file — fall back to defaults so we don't wedge the gateway.
        return NetworkState(peer_id=str(uuid.uuid4()), name=_default_name())
    return NetworkState.from_dict(data if isinstance(data, dict) else {})


def save_state(state: NetworkState, path: Path | None = None) -> None:
    """Atomically write network state to disk with 0600 permissions."""
    p = path or NETWORK_STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def get_or_create_identity(path: Path | None = None) -> NetworkState:
    """Load state, creating + persisting a default identity on first run."""
    state = load_state(path)
    if not state.peer_id or not state.name:
        state.peer_id = state.peer_id or str(uuid.uuid4())
        state.name = state.name or _default_name()
        save_state(state, path)
    return state


def new_secret() -> str:
    """Generate a 32-byte HMAC secret as hex."""
    return secrets.token_hex(32)
