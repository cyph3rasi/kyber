"""Shared slash-command vocabulary for every Kyber surface.

Every channel — Discord, Telegram, WhatsApp, dashboard chat, CLI REPL,
TUI — runs user input through :func:`dispatch`, which returns either a
:class:`CommandResult` with a reply to send back OR ``None`` if the input
wasn't a slash command (and the channel should hand it to the LLM).

Commands live in :mod:`kyber.commands.builtin` (imported lazily so the
registry is populated exactly once, the first time ``dispatch`` runs).

Commands take a :class:`CommandContext` describing who invoked them and
where. Channels populate the context; commands don't need to know the
difference between a Discord DM and a dashboard chat.
"""

from kyber.commands.registry import (
    Command,
    CommandContext,
    CommandResult,
    available_command_names,
    dispatch,
    is_slash_command,
    list_commands,
    register,
)

__all__ = [
    "Command",
    "CommandContext",
    "CommandResult",
    "available_command_names",
    "dispatch",
    "is_slash_command",
    "list_commands",
    "register",
]
