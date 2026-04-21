"""Shared RPC dispatch for Kyber network nodes.

Both host and spoke receive ``rpc_request`` envelopes and respond. Methods:

* ``ping`` — liveness check; returns ``{ok, ts, peer_name}``.
* ``notebook.*`` — host-only, read/write the shared store.
* ``network.list_peers`` — returns every peer this node knows about so
  agents can discover who they can invoke on.
* ``tool.invoke`` — execute a locally-registered tool, subject to the
  ``network.exposed_tools`` allowlist in config.

When a spoke wants to target another spoke, it sets ``target`` on the
request payload. The host sees ``target`` and relays: it forwards the
request to the target spoke, awaits the response, and sends it back to
the origin.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kyber.network.protocol import Envelope, make_rpc_response

logger = logging.getLogger(__name__)


def _allowed_tools() -> set[str]:
    try:
        from kyber.config.loader import load_config

        cfg = load_config()
        return set(cfg.network.exposed_tools or [])
    except Exception:  # pragma: no cover
        return set()


def _self_info() -> dict[str, Any]:
    from kyber.network.state import load_state

    state = load_state()
    return {"peer_id": state.peer_id, "name": state.name, "role": state.role}


def _all_peers_known() -> list[dict[str, Any]]:
    """Return every peer this node knows about (including self).

    Hosts know all spokes. Spokes only know themselves + the host after
    handshake; to learn about sibling spokes a spoke has to call
    ``network.list_peers`` against the host.
    """
    from kyber.network.state import ROLE_HOST, ROLE_SPOKE, load_state

    state = load_state()
    out: list[dict[str, Any]] = [
        {"peer_id": state.peer_id, "name": state.name, "role": state.role, "self": True}
    ]
    if state.role == ROLE_HOST:
        for p in state.paired_peers:
            out.append(
                {
                    "peer_id": p.peer_id,
                    "name": p.name,
                    "role": p.role,
                    "self": False,
                }
            )
    elif state.role == ROLE_SPOKE and state.host_peer is not None:
        out.append(
            {
                "peer_id": state.host_peer.peer_id,
                "name": state.host_peer.name,
                "role": state.host_peer.role,
                "self": False,
            }
        )
    return out


async def execute_local_rpc(
    method: str,
    params: dict[str, Any],
    *,
    caller_peer_id: str = "",
    caller_name: str = "",
) -> Any:
    """Execute an RPC method locally and return the result.

    Raises ``ValueError`` for bad params and ``RuntimeError`` for forbidden
    or unknown methods. The caller converts those into structured rpc
    errors. ``caller_peer_id`` / ``caller_name`` identify who asked — used
    for audit logging and passed into tool execution context.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    if method == "ping":
        info = _self_info()
        return {"ok": True, "ts": time.time(), "peer_name": info["name"]}

    if method == "network.list_peers":
        return {"peers": _all_peers_known()}

    if method == "network.self":
        return _self_info()

    if method.startswith("notebook."):
        # Notebook still only lives on host; if we're a spoke we get here
        # due to a misrouted call. Return a clear error.
        from kyber.network.state import ROLE_HOST, load_state

        if load_state().role != ROLE_HOST:
            raise RuntimeError("notebook.* must target the host")
        from kyber.network.host import _handle_notebook_rpc

        class _ProxyPeer:
            def __init__(self, pid: str, name: str) -> None:
                self.peer_id = pid or "unknown"
                self.name = name or "unknown"

        return _handle_notebook_rpc(
            method, params, _ProxyPeer(caller_peer_id, caller_name)
        )

    if method == "tool.invoke":
        tool_name = str(params.get("tool_name") or "").strip()
        tool_params = params.get("params") or {}
        if not isinstance(tool_params, dict):
            raise ValueError("tool.invoke params.params must be an object")
        if not tool_name:
            raise ValueError("tool_name is required")

        allowed = _allowed_tools()
        # ``*`` means "expose everything registered". Explicit tool names
        # still take precedence if both are listed — either mode allows
        # the invocation. An empty allow-list disables remote invocation.
        if "*" not in allowed and tool_name not in allowed:
            raise RuntimeError(
                f"tool {tool_name!r} is not in this node's exposed_tools allowlist. "
                f"Allowed: {sorted(allowed) or '(none)'}. Add it to "
                "config.network.exposedTools on the target machine, or set the "
                "list to [\"*\"] to expose every registered tool."
            )

        from kyber.agent.tools.registry import registry

        tool = registry.get(tool_name)
        if tool is None:
            raise RuntimeError(f"tool {tool_name!r} is not registered on this node")

        errors = tool.validate_params(tool_params)
        if errors:
            raise ValueError("invalid params: " + "; ".join(errors))

        logger.info(
            "network: %s@%s invoked tool %s(%s)",
            caller_name or "?",
            caller_peer_id[:8] if caller_peer_id else "?",
            tool_name,
            ", ".join(f"{k}={v!r}" for k, v in tool_params.items())[:200],
        )
        try:
            output = await tool.execute(**tool_params)
        except Exception as e:  # surfaced as rpc error
            raise RuntimeError(f"{tool_name} raised {type(e).__name__}: {e}") from e
        return {"tool_name": tool_name, "output": str(output)}

    raise RuntimeError(f"unknown RPC method {method!r}")


async def build_rpc_response(
    env: Envelope,
    *,
    caller_peer_id: str,
    caller_name: str,
    relay_fn=None,
) -> Envelope:
    """Dispatch an incoming ``rpc_request`` and build the response envelope.

    If ``relay_fn`` is provided and the request carries a ``target`` field
    addressing a different peer, we call ``relay_fn(target, new_request)``
    instead of executing locally. That's how the host turns spoke-to-spoke
    calls into forwarded pairs of RPCs.
    """
    payload = env.payload or {}
    method = str(payload.get("method") or "")
    params = payload.get("params")
    target = str(payload.get("target") or "")

    if target and relay_fn is not None:
        try:
            result = await relay_fn(target, method, params, caller_peer_id, caller_name)
            return make_rpc_response(env.id, result=result)
        except ValueError as e:
            return make_rpc_response(
                env.id, error={"code": "bad_request", "message": str(e)}
            )
        except RuntimeError as e:
            return make_rpc_response(
                env.id, error={"code": "relay_error", "message": str(e)}
            )
        except Exception as e:  # pragma: no cover
            logger.exception("network: relay of %s to %s failed", method, target)
            return make_rpc_response(
                env.id,
                error={"code": "internal", "message": f"{type(e).__name__}: {e}"},
            )

    try:
        result = await execute_local_rpc(
            method,
            params if isinstance(params, dict) else {},
            caller_peer_id=caller_peer_id,
            caller_name=caller_name,
        )
    except ValueError as e:
        return make_rpc_response(env.id, error={"code": "bad_request", "message": str(e)})
    except RuntimeError as e:
        return make_rpc_response(env.id, error={"code": "forbidden", "message": str(e)})
    except Exception as e:  # pragma: no cover
        logger.exception("network: rpc %s failed", method)
        return make_rpc_response(
            env.id, error={"code": "internal", "message": f"{type(e).__name__}: {e}"}
        )
    return make_rpc_response(env.id, result=result)
