"""Wire protocol for Kyber network links.

Messages are JSON envelopes sent over a WebSocket:

.. code-block:: text

    { "type": str,       # e.g. "hello", "heartbeat", "rpc_request"
      "id":   str,       # random id; on rpc_response, echoes the request id
      "ts":   float,     # seconds since epoch, for replay protection
      "payload": dict,
      "sig": str }       # HMAC-SHA256(secret, compact_serialized_body)

Auth works as follows:

1. Before sending, the sender serializes ``{type,id,ts,payload}`` with
   canonical JSON (sorted keys, no whitespace), computes an HMAC-SHA256
   of the bytes using the shared peer secret, and appends it as ``sig``.
2. The receiver recomputes the HMAC and compares with
   :func:`hmac.compare_digest`. Messages older than ``MAX_MESSAGE_AGE_S``
   are rejected as replays.

Message types:

* ``hello`` / ``hello_ack`` — connection handshake.
* ``heartbeat`` / ``heartbeat_ack`` — keepalive.
* ``rpc_request`` / ``rpc_response`` — notebook.*, ping, and (Phase 3) tool
  invocation. Payload is ``{method, params}`` on request and either
  ``{result}`` or ``{error: {code, message}}`` on response. Response uses
  the request's envelope id so the client can match them up.
* ``name_update`` — notification sent when a peer renames itself. Payload
  ``{peer_id, name}``. The receiver updates its local record for that peer
  so display names stay in sync across the network without a reconnect.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

# Reject any signed message older than this, to cap replay risk if a spoke's
# secret is briefly exposed (and to cover big clock skew politely).
MAX_MESSAGE_AGE_S = 300.0


@dataclass
class Envelope:
    type: str
    id: str
    ts: float
    payload: dict[str, Any]

    def to_json(self, secret: str) -> str:
        body = self._body()
        sig = _sign(secret, body)
        return json.dumps({**json.loads(body), "sig": sig})

    def _body(self) -> str:
        # Canonical form: sorted keys, no whitespace. Same function the
        # receiver uses to verify.
        return json.dumps(
            {"type": self.type, "id": self.id, "ts": self.ts, "payload": self.payload},
            sort_keys=True,
            separators=(",", ":"),
        )


class ProtocolError(RuntimeError):
    """Raised when a WebSocket frame fails parsing, signing, or freshness checks."""


def make_envelope(type_: str, payload: dict[str, Any] | None = None) -> Envelope:
    return Envelope(
        type=type_,
        id=secrets.token_hex(8),
        ts=time.time(),
        payload=dict(payload or {}),
    )


def make_rpc_request(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    target: str | None = None,
) -> Envelope:
    """Build an ``rpc_request`` envelope. The id is the correlation key.

    ``target`` goes at the top level of the payload (not inside ``params``)
    so the receiver's relay check (``payload.get('target')``) can see it.
    Setting target on a spoke→host request asks the host to forward the
    call to that peer_id instead of executing it locally.
    """
    payload: dict[str, Any] = {"method": method, "params": dict(params or {})}
    if target:
        payload["target"] = target
    return make_envelope("rpc_request", payload)


def make_rpc_response(
    request_id: str,
    *,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> Envelope:
    """Build an ``rpc_response`` envelope that replies to a specific request id."""
    payload: dict[str, Any] = {}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    env = Envelope(type="rpc_response", id=request_id, ts=time.time(), payload=payload)
    return env


def encode_signed(env: Envelope, secret: str) -> str:
    """Serialize and sign an envelope with the given HMAC key (hex)."""
    return env.to_json(secret)


def decode_signed(
    raw: str, secret: str, *, now: float | None = None
) -> Envelope:
    """Parse and verify a signed envelope. Raises ProtocolError on any failure."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ProtocolError("envelope must be a JSON object")

    sig = data.get("sig")
    if not isinstance(sig, str) or not sig:
        raise ProtocolError("missing signature")

    body_dict = {k: data[k] for k in ("type", "id", "ts", "payload") if k in data}
    if set(body_dict) != {"type", "id", "ts", "payload"}:
        raise ProtocolError("envelope missing required fields")
    if not isinstance(body_dict["payload"], dict):
        raise ProtocolError("payload must be an object")
    body = json.dumps(body_dict, sort_keys=True, separators=(",", ":"))

    expected = _sign(secret, body)
    if not hmac.compare_digest(sig, expected):
        raise ProtocolError("signature mismatch")

    ts = body_dict["ts"]
    if not isinstance(ts, (int, float)):
        raise ProtocolError("ts must be a number")
    current = now if now is not None else time.time()
    if abs(current - float(ts)) > MAX_MESSAGE_AGE_S:
        raise ProtocolError("message outside freshness window")

    return Envelope(
        type=str(body_dict["type"]),
        id=str(body_dict["id"]),
        ts=float(ts),
        payload=body_dict["payload"],
    )


def _sign(secret_hex: str, body: str) -> str:
    try:
        key = bytes.fromhex(secret_hex)
    except ValueError as e:
        raise ProtocolError(f"invalid secret format: {e}") from e
    mac = hmac.new(key, body.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()
