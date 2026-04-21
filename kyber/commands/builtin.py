"""Built-in Kyber slash commands — available in every channel.

Each command is small by design and pulls live state from config / the
task registry / network state at call time, so the answers stay accurate
as the user edits things between messages.

Conventions:

* Reply text is markdown. Every channel we support (Discord, Telegram
  with parse mode, dashboard chat, CLI Rich) renders or degrades from
  markdown cleanly.
* Side-effects that need the channel to react (clearing history, exiting
  the REPL) are expressed on the :class:`CommandResult` rather than done
  inline — that keeps the handlers pure-ish and testable.
"""

from __future__ import annotations

import logging
from typing import Any

from kyber.session.manager import Session

from kyber.commands.registry import (
    CommandContext,
    CommandResult,
    list_commands,
    register,
)

logger = logging.getLogger(__name__)


def _current_agent():
    """Best-effort lookup of the live AgentCore for this process.

    Channel-supplied ``ctx.agent`` is authoritative; this is a fallback
    for channels that were hooked up before ``ChannelManager.attach_agent``
    ran, or for non-channel invokers (cron jobs, dashboard). Returns
    None if no AgentCore has been constructed in this process yet.
    """
    try:
        from kyber.agent.core import get_current_agent

        return get_current_agent()
    except Exception:
        return None


# ── /help ─────────────────────────────────────────────────────────────


@register("help", summary="List available slash commands", aliases=("?",))
async def cmd_help(args: list[str], ctx: CommandContext) -> CommandResult:
    lines = ["**Slash commands**", ""]
    for cmd in list_commands():
        label = f"`/{cmd.name}"
        if cmd.usage:
            label += f" {cmd.usage}"
        label += "`"
        if cmd.aliases:
            label += f"  _(aliases: {', '.join('/' + a for a in cmd.aliases)})_"
        lines.append(f"- {label} — {cmd.summary}")
    return CommandResult(reply_text="\n".join(lines))


# ── /new, /reset ──────────────────────────────────────────────────────


@register(
    "new",
    summary="Clear this conversation's history and start fresh",
    aliases=("reset",),
)
async def cmd_new(args: list[str], ctx: CommandContext) -> CommandResult:
    """Clear the current session's history and reset its /usage boundary.

    Tries (in order):
      1. ``agent.reset_session(key)`` — the idiomatic AgentCore hook (added
         in 2026.4.21.55; older gateways silently skipped this step, which
         is why ``/usage`` kept growing across every "reset").
      2. ``agent.sessions.clear(key)`` — direct SessionManager call. Works
         even on agents without ``reset_session`` as long as the new
         ``clear`` method is present.
      3. ``agent.session_store.clear(key)`` — legacy fallback kept for any
         external AgentCore replacement.
    """
    cleared_path: str | None = None
    new_boundary = None

    if ctx.agent is not None and ctx.session_key:
        try:
            reset = getattr(ctx.agent, "reset_session", None)
            if callable(reset):
                maybe_coro = reset(ctx.session_key)
                if hasattr(maybe_coro, "__await__"):
                    await maybe_coro
                cleared_path = "reset_session"
            else:
                sessions = getattr(ctx.agent, "sessions", None)
                sm_clear = getattr(sessions, "clear", None) if sessions is not None else None
                if callable(sm_clear):
                    sm_clear(ctx.session_key)
                    cleared_path = "sessions.clear"
                else:
                    store = getattr(ctx.agent, "session_store", None)
                    legacy_clear = getattr(store, "clear", None) if store is not None else None
                    if callable(legacy_clear):
                        legacy_clear(ctx.session_key)
                        cleared_path = "session_store.clear"
        except Exception as e:
            logger.exception("session reset failed")
            return CommandResult(reply_text=f"Reset failed: {e}")

        # Read the new boundary back so the user can verify the reset moved.
        if cleared_path:
            try:
                sessions = getattr(ctx.agent, "sessions", None)
                if sessions is not None:
                    sess = sessions.get_or_create(ctx.session_key)
                    new_boundary = getattr(sess, "started_at", None)
            except Exception:
                new_boundary = None

    if cleared_path:
        stamp = new_boundary.strftime("%H:%M:%S") if new_boundary is not None else "now"
        return CommandResult(
            reply_text=(
                "✓ Session cleared. `/usage` counter reset; next message "
                f"starts a fresh conversation. (boundary: {stamp})"
            ),
            reset_session=True,
        )
    return CommandResult(
        reply_text=(
            "Session cleared locally, but the gateway didn't acknowledge "
            "it — `/usage` may still show carry-over. Try `kyber upgrade` "
            "and then `/new` again."
        ),
        reset_session=True,
    )


# ── /quit, /exit (CLI-only; harmless elsewhere) ──────────────────────


@register("quit", summary="Exit the chat REPL (CLI only)", aliases=("exit",))
async def cmd_quit(args: list[str], ctx: CommandContext) -> CommandResult:
    if ctx.channel in ("cli", "tui"):
        return CommandResult(should_exit=True)
    return CommandResult(reply_text="`/quit` only applies to the CLI REPL.")


# ── /session ─────────────────────────────────────────────────────────


@register("session", summary="Show or switch the current session id", usage="[id]")
async def cmd_session(args: list[str], ctx: CommandContext) -> CommandResult:
    if not args:
        return CommandResult(reply_text=f"Session: `{ctx.session_id}`")
    new_id = args[0].strip()
    if not new_id:
        return CommandResult(reply_text="Session id can't be empty.")
    if ctx.channel not in ("cli", "tui"):
        return CommandResult(
            reply_text=(
                "Switching sessions from chat isn't supported on this channel — "
                "each Discord/Telegram/dashboard chat has its own session by default."
            )
        )
    return CommandResult(
        reply_text=f"✓ Switched session to `{new_id}`.",
        new_session_id=new_id,
    )


# ── /model ───────────────────────────────────────────────────────────


@register(
    "model",
    summary="Show or switch the active LLM model",
    usage="[name]",
)
async def cmd_model(args: list[str], ctx: CommandContext) -> CommandResult:
    from kyber.config.loader import load_config, save_config

    cfg = load_config()
    details = cfg.get_provider_details()
    current_provider = details.get("configured_provider_name") or details.get("provider_name")
    current_model = details.get("model") or cfg.get_model()

    if not args:
        return CommandResult(
            reply_text=(
                f"**Model**: `{current_model}`\n"
                f"**Provider**: `{current_provider}`\n\n"
                "Switch with `/model <name>`. "
                "(Changes persist to your config and take effect on the next turn.)"
            )
        )

    requested = " ".join(args).strip()
    provider_name = str(current_provider or "").strip().lower()

    try:
        if provider_name == "chatgpt_subscription":
            cfg.providers.chatgpt_subscription.model = requested
        else:
            prov = getattr(cfg.providers, provider_name, None)
            if prov is None:
                for cp in cfg.providers.custom:
                    if cp.name.strip().lower() == provider_name:
                        cp.model = requested
                        break
                else:
                    return CommandResult(
                        reply_text=(
                            f"Can't change model — unknown provider "
                            f"`{provider_name}`. Edit config manually."
                        )
                    )
            else:
                setattr(prov, "model", requested)
        save_config(cfg)
    except Exception as e:
        logger.exception("model swap failed")
        return CommandResult(reply_text=f"Couldn't save model: {e}")

    return CommandResult(
        reply_text=(
            f"✓ Model set to `{requested}` for provider `{provider_name}`. "
            "Your next message uses the new model."
        )
    )


# ── /skills ──────────────────────────────────────────────────────────


@register("skills", summary="List installed skills", usage="[search]")
async def cmd_skills(args: list[str], ctx: CommandContext) -> CommandResult:
    try:
        from kyber.agent.skills import SkillsLoader
        from kyber.config.loader import load_config

        cfg = load_config()
        workspace = cfg.workspace_path
        loader = SkillsLoader(workspace)
        summary = loader.build_skills_summary()
    except Exception as e:
        return CommandResult(reply_text=f"Couldn't read skills: {e}")
    if not summary:
        return CommandResult(reply_text="No skills installed yet.")
    if args:
        needle = " ".join(args).lower()
        matching = [
            line for line in summary.splitlines() if needle in line.lower()
        ]
        if not matching:
            return CommandResult(reply_text=f"No skills match `{needle}`.")
        return CommandResult(reply_text="\n".join(matching))
    return CommandResult(reply_text=summary)


# ── /usage ───────────────────────────────────────────────────────────


@register("usage", summary="Show token usage + estimated cost for session + today")
async def cmd_usage(args: list[str], ctx: CommandContext) -> CommandResult:
    agent = ctx.agent or _current_agent()
    if agent is None:
        return CommandResult(
            reply_text=(
                "Usage totals require the gateway's in-memory agent. If "
                "you're hitting this from a channel, the gateway may have "
                "restarted since that channel connected — send another "
                "message or restart the service."
            )
        )
    ctx.agent = agent

    try:
        stats = _aggregate_usage(agent)
    except Exception as e:
        return CommandResult(reply_text=f"Couldn't compute usage: {e}")

    session_key = ctx.session_key or ""
    today = stats["today"]

    # "This session" is the post-/new bucket: only tasks created after the
    # session's last reset boundary count. Without this filter, Discord users
    # saw a monotonically-growing counter per channel because session_key is
    # sticky (channel_id) and /new only clears history.
    session_started_at = None
    if hasattr(agent, "sessions") and session_key:
        try:
            sess = agent.sessions.get_or_create(session_key)
            session_started_at = getattr(sess, "started_at", None)
        except Exception:
            session_started_at = None

    session = _aggregate_session_usage(
        agent, session_key, since=session_started_at
    )

    # Label the session bucket with the /new boundary so the user can tell
    # "this session" from "this channel all-time". Without this, 5k tokens
    # after /new looks identical to 5k tokens after days of chatting and
    # you can't tell whether /new took effect.
    session_label = "This session"
    if session_started_at is not None:
        session_label = f"This session (since {session_started_at.strftime('%H:%M:%S')})"

    lines = [
        "**Token usage**",
        _format_bucket_line(session_label, session, fallback=session_key or "—"),
        _format_bucket_line("Today (all channels)", today),
    ]
    top = sorted(
        stats["by_session"].items(),
        key=lambda kv: kv[1]["total"],
        reverse=True,
    )[:5]
    if top:
        lines.append("")
        lines.append("**Top sessions**")
        for key, b in top:
            lines.append(f"- `{key}`: " + _format_bucket_body(b))
    return CommandResult(reply_text="\n".join(lines))


# ── /cancel ──────────────────────────────────────────────────────────


@register("cancel", summary="Cancel the currently running task in this session")
async def cmd_cancel(args: list[str], ctx: CommandContext) -> CommandResult:
    agent = ctx.agent or _current_agent()
    if agent is None:
        return CommandResult(reply_text="No running agent to cancel against.")
    ctx.agent = agent
    try:
        registry = getattr(ctx.agent, "registry", None)
        if registry is None:
            return CommandResult(reply_text="Task registry unavailable.")
        active = list(registry.get_active_tasks())
        mine = [
            t for t in active
            if (t.origin_channel == ctx.channel and t.origin_chat_id == ctx.session_id)
        ]
        target = mine[0] if mine else (active[0] if active else None)
        if target is None:
            return CommandResult(reply_text="Nothing running right now.")
        cancel = getattr(ctx.agent, "_cancel_task", None)
        ok = False
        if callable(cancel):
            ok = bool(cancel(target.id))
        if not ok:
            # mark directly as a fallback so the UI doesn't get stuck
            registry.mark_cancelled(target.id, "Cancelled by user via /cancel")
            ok = True
        return CommandResult(
            reply_text=f"✓ Cancelled task `{target.reference or target.id}`."
        )
    except Exception as e:
        logger.exception("cancel failed")
        return CommandResult(reply_text=f"Cancel failed: {e}")


# ── /peers ───────────────────────────────────────────────────────────


@register(
    "peers",
    summary="List Kyber instances paired with this network",
    aliases=("network",),
)
async def cmd_peers(args: list[str], ctx: CommandContext) -> CommandResult:
    try:
        from kyber.network.rpc import _all_peers_known
        from kyber.network.state import ROLE_SPOKE, load_state

        state = load_state()
        peers: list[dict[str, Any]] = _all_peers_known()
        # Spokes only know host locally; ask the host for sibling spokes.
        if state.role == ROLE_SPOKE:
            try:
                from kyber.network.spoke import get_spoke_client

                client = get_spoke_client()
                if client.status.get("connected"):
                    remote = await client.call_rpc("network.list_peers", {}, timeout=5.0)
                    peers = (remote or {}).get("peers") or peers
            except Exception:
                pass
    except Exception as e:
        return CommandResult(reply_text=f"Couldn't read network state: {e}")

    if not peers:
        return CommandResult(reply_text="This Kyber isn't on a network yet.")
    lines = ["**Paired peers**", ""]
    for p in peers:
        marker = " (this machine)" if p.get("self") else ""
        lines.append(
            f"- **{p.get('name', '?')}**{marker} · role `{p.get('role', '?')}`"
        )
    return CommandResult(reply_text="\n".join(lines))


# ── Stubs still "coming soon" — clearer than silent failure ──────────


def _stub(name: str, blurb: str):
    async def _h(args: list[str], ctx: CommandContext) -> CommandResult:
        return CommandResult(
            reply_text=(
                f"`/{name}` isn't implemented yet. {blurb}"
            )
        )

    return _h


register(
    "compress",
    summary="Summarize the conversation so far and reset the context (coming soon)",
)(_stub("compress", "Next release will LLM-summarize history and start fresh."))


@register("cost", summary="Estimated USD cost of recent LLM activity")
async def cmd_cost(args: list[str], ctx: CommandContext) -> CommandResult:
    agent = ctx.agent or _current_agent()
    if agent is None:
        return CommandResult(
            reply_text="No running agent — can't price anything yet."
        )
    try:
        stats = _aggregate_usage(agent)
    except Exception as e:
        return CommandResult(reply_text=f"Couldn't compute cost: {e}")

    today = stats["today"]
    lines = ["**Estimated cost**"]
    if today["subscription_hits"] and today["total"] > 0:
        lines.append(
            f"- Today: {today['total']:,} tokens "
            f"({today['subscription_hits']:,} via subscription, "
            "no per-token charge)"
        )
    else:
        lines.append(
            f"- Today: **{_format_usd_or_dash(today['usd'])}** "
            f"({today['total']:,} tokens)"
        )

    # Per-model breakdown today so the user can see where the spend went.
    if stats["by_model_today"]:
        lines.append("")
        lines.append("**By model today**")
        by_model_sorted = sorted(
            stats["by_model_today"].items(),
            key=lambda kv: kv[1]["total"],
            reverse=True,
        )
        for model, b in by_model_sorted[:8]:
            badge = "(subscription)" if b["subscription_hits"] else _format_usd_or_dash(b["usd"])
            lines.append(
                f"- `{model or 'unknown'}`: {b['total']:,} tokens — {badge}"
            )
    return CommandResult(reply_text="\n".join(lines))


# ── Usage/cost helpers ──────────────────────────────────────────────


def _empty_bucket() -> dict[str, int | float]:
    return {
        "input": 0,
        "output": 0,
        "total": 0,
        "usd": 0.0,
        "subscription_hits": 0,  # tasks whose provider was a flat-fee subscription
    }


def _aggregate_usage(agent) -> dict[str, Any]:
    """Walk the task registry and bucket token + cost by session / model / day."""
    from kyber.commands.pricing import estimate_cost, is_subscription_provider
    import datetime as _dt

    by_session: dict[str, dict[str, Any]] = {}
    by_model_today: dict[str, dict[str, Any]] = {}
    today_bucket = _empty_bucket()

    today = _dt.date.today()

    registry = agent.registry
    tasks = list(registry.get_active_tasks()) + list(registry.get_history(limit=500))

    for t in tasks:
        in_tok = int(getattr(t, "input_tokens", 0) or 0)
        out_tok = int(getattr(t, "output_tokens", 0) or 0)
        total_tok = int(getattr(t, "total_tokens", 0) or 0) or (in_tok + out_tok)
        if total_tok <= 0:
            continue

        model = str(getattr(t, "model", "") or "")
        provider = str(getattr(t, "provider", "") or "")
        cost = estimate_cost(
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=model,
            provider=provider,
        )
        usd = cost["usd"] if cost["usd"] is not None else 0.0
        is_sub = bool(cost["subscription"])

        # Per-session accumulation.
        key = f"{t.origin_channel}:{t.origin_chat_id or ''}"
        sb = by_session.setdefault(key, _empty_bucket())
        sb["input"] += in_tok
        sb["output"] += out_tok
        sb["total"] += total_tok
        sb["usd"] += usd
        if is_sub:
            sb["subscription_hits"] += 1

        # Today-only buckets.
        created = getattr(t, "created_at", None)
        if created is not None and created.date() == today:
            today_bucket["input"] += in_tok
            today_bucket["output"] += out_tok
            today_bucket["total"] += total_tok
            today_bucket["usd"] += usd
            if is_sub:
                today_bucket["subscription_hits"] += 1

            mb = by_model_today.setdefault(model, _empty_bucket())
            mb["input"] += in_tok
            mb["output"] += out_tok
            mb["total"] += total_tok
            mb["usd"] += usd
            if is_sub:
                mb["subscription_hits"] += 1

    return {
        "by_session": by_session,
        "today": today_bucket,
        "by_model_today": by_model_today,
    }


def _aggregate_session_usage(
    agent,
    session_key: str,
    *,
    since=None,
) -> dict[str, Any]:
    """Bucket token usage for a single session, optionally bounded by ``since``.

    ``since`` is a ``datetime`` — tasks created before this timestamp are
    ignored. Used by ``/usage`` to scope "This session" to tasks created
    after the most recent ``/new``. Passing ``since=None`` falls through
    to the all-time total for that session.
    """
    from kyber.commands.pricing import estimate_cost

    bucket = _empty_bucket()
    if not session_key:
        return bucket

    registry = agent.registry
    tasks = list(registry.get_active_tasks()) + list(registry.get_history(limit=500))

    channel, _, chat_id = session_key.partition(":")
    for t in tasks:
        if (getattr(t, "origin_channel", "") or "") != channel:
            continue
        if (getattr(t, "origin_chat_id", "") or "") != chat_id:
            continue

        created = getattr(t, "created_at", None)
        if since is not None and created is not None and created < since:
            continue

        in_tok = int(getattr(t, "input_tokens", 0) or 0)
        out_tok = int(getattr(t, "output_tokens", 0) or 0)
        total_tok = int(getattr(t, "total_tokens", 0) or 0) or (in_tok + out_tok)
        if total_tok <= 0:
            continue

        cost = estimate_cost(
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=str(getattr(t, "model", "") or ""),
            provider=str(getattr(t, "provider", "") or ""),
        )
        bucket["input"] += in_tok
        bucket["output"] += out_tok
        bucket["total"] += total_tok
        bucket["usd"] += cost["usd"] if cost["usd"] is not None else 0.0
        if cost["subscription"]:
            bucket["subscription_hits"] += 1

    return bucket


def _format_bucket_body(b: dict[str, Any]) -> str:
    from kyber.commands.pricing import format_usd

    core = (
        f"**{b['total']:,}** tokens "
        f"(in {b['input']:,} · out {b['output']:,})"
    )
    if b.get("subscription_hits") and b.get("total", 0) > 0 and not b.get("usd"):
        return core + " — subscription"
    if b.get("usd", 0) > 0:
        return core + f" · {format_usd(b['usd'])}"
    return core


def _format_bucket_line(label: str, b: dict[str, Any], *, fallback: str | None = None) -> str:
    tag = f" (`{fallback}`)" if fallback and fallback != "—" else ""
    return f"- {label}{tag}: {_format_bucket_body(b)}"


def _count_tokens_in_session(session: "Session") -> dict[str, int]:
    """Count approximate tokens from persisted messages in a Session.

    Very rough heuristic (4 chars ≈ 1 token). Only counts user + assistant
    messages (ignores tool output and system messages).
    """
    input_t = 0
    output_t = 0

    for msg in session.messages:
        role = msg.get("role", "")
        content = str(msg.get("content", "") or "")
        if not content.strip():
            continue
        # Very loose approximation
        approx = len(content) // 4 + 1
        if role == "user":
            input_t += approx
        elif role == "assistant":
            output_t += approx

    return {
        "input": input_t,
        "output": output_t,
        "total": input_t + output_t,
    }


def _format_usd_or_dash(usd: float | None) -> str:
    from kyber.commands.pricing import format_usd

    return format_usd(usd)
