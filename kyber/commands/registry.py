"""Channel-agnostic slash-command registry + dispatcher.

Runs in every process that handles user input — gateway (for dashboard chat
and every messaging channel) and the CLI REPL / TUI. The contract is the
same everywhere:

1. Channel receives user text.
2. It builds a :class:`CommandContext` with who/where.
3. It calls :func:`dispatch(text, ctx)`.
4. If the text was a slash command, ``dispatch`` returns a
   :class:`CommandResult` with a reply to send back (and optional side-
   effects like a session reset). The channel shows the reply and STOPS —
   the LLM is not invoked.
5. If the text was plain prose, ``dispatch`` returns ``None`` and the
   channel hands off to the LLM as before.

Handlers are registered via :func:`register` and live in
:mod:`kyber.commands.builtin`. Importing this module triggers discovery
exactly once — safe to call ``dispatch`` from anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class CommandContext:
    """Everything a command handler might need to know about its invocation.

    Populated by whichever channel received the user's message. Commands
    should read what they need and tolerate missing fields — e.g. Discord
    won't set ``chat_title`` in a DM. The channel-agnostic bits
    (session_id, peer, rendering) are the contract; everything else is
    best-effort.
    """

    # Identity of the invocation
    channel: str = ""            # "discord" | "telegram" | "whatsapp" | "dashboard" | "cli" | "tui"
    session_id: str = ""         # unique per conversation thread on that channel
    sender_id: str = ""          # channel-native user id
    sender_name: str = ""        # channel-native display name

    # Session / agent state (populated when the channel has access)
    agent: Any = None            # AgentCore if available (gateway side)
    session_key: str = ""        # "<channel>:<session_id>" convention

    # Config (fresh load so commands see the latest values)
    config: Any = None

    # HTTP client for commands that need to poke the gateway
    http_client: Any = None

    # Per-channel rendering hints — Discord supports markdown, CLI pipes
    # through rich, dashboard shows markdown. Commands can use these to
    # pick verbosity / formatting.
    supports_markdown: bool = True

    # Free-form extras for channel-specific things (e.g. Discord
    # message object, Telegram update). Commands should not rely on
    # anything here; use typed fields above.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandResult:
    """What a command tells its channel to do next.

    Every field is optional — the minimum is usually just ``reply_text``.
    """

    # Plain text / markdown to send back to the user on the origin channel.
    reply_text: str = ""

    # If set, channel should forget this session (reset history).
    reset_session: bool = False

    # If set, channel should switch to this session id (CLI/TUI only).
    new_session_id: str | None = None

    # Signal to the CLI REPL / TUI to exit.
    should_exit: bool = False


CommandHandler = Callable[[list[str], CommandContext], Awaitable[CommandResult]]


@dataclass
class Command:
    name: str
    summary: str
    handler: CommandHandler
    usage: str = ""
    aliases: tuple[str, ...] = ()


_COMMANDS: dict[str, Command] = {}
_DISCOVERED = False


def register(
    name: str,
    summary: str,
    usage: str = "",
    aliases: tuple[str, ...] = (),
):
    """Decorator: register a slash command under ``name`` (and any aliases)."""

    def wrap(fn: CommandHandler) -> CommandHandler:
        cmd = Command(
            name=name, summary=summary, handler=fn, usage=usage, aliases=aliases
        )
        _COMMANDS[name.lower()] = cmd
        for alias in aliases:
            _COMMANDS[alias.lower()] = cmd
        return fn

    return wrap


def _ensure_discovered() -> None:
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    # Import triggers registration via module-level @register decorators.
    from kyber.commands import builtin  # noqa: F401


def available_command_names() -> list[str]:
    """Names that pass tab-completion. Aliases are included."""
    _ensure_discovered()
    return sorted(_COMMANDS.keys())


def list_commands() -> list[Command]:
    """Distinct commands (each command appears once even if it has aliases)."""
    _ensure_discovered()
    seen_ids: set[int] = set()
    out: list[Command] = []
    for name in sorted(_COMMANDS.keys()):
        cmd = _COMMANDS[name]
        if id(cmd) in seen_ids:
            continue
        seen_ids.add(id(cmd))
        out.append(cmd)
    return out


def is_slash_command(text: str) -> bool:
    """True when ``text`` looks like a slash-prefixed command.

    Cheap check channels can use to decide whether to dispatch. We require
    the slash to be the very first character and some command content to
    follow — ``"/ foo"`` doesn't count (user probably hit the slash key by
    accident).
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False
    if len(stripped) < 2 or stripped[1].isspace():
        return False
    return True


async def dispatch(text: str, ctx: CommandContext) -> CommandResult | None:
    """Dispatch a slash command. Returns None if ``text`` isn't one.

    Every built-in command is shielded against exceptions — a buggy
    handler produces a ``[red]/foo failed: …[/red]`` reply instead of
    propagating upward and killing the channel's message loop.
    """
    _ensure_discovered()
    if not is_slash_command(text):
        return None

    stripped = text.strip()[1:]  # drop leading '/'
    try:
        tokens = shlex.split(stripped)
    except ValueError as e:
        return CommandResult(reply_text=f"Command parse error: {e}")
    if not tokens:
        return CommandResult(reply_text="Empty command. Type `/help` to see what's available.")

    name, args = tokens[0].lower(), tokens[1:]
    cmd = _COMMANDS.get(name)
    if cmd is None:
        return CommandResult(
            reply_text=(
                f"Unknown command: `/{name}`. Try `/help` for the list of commands."
            )
        )
    try:
        return await cmd.handler(args, ctx)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # pragma: no cover — defensive
        logger.exception("slash command /%s crashed", name)
        return CommandResult(reply_text=f"`/{name}` failed: {e}")
