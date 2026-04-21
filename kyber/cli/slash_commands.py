"""CLI REPL/TUI adapter for the shared slash-command registry.

The registry itself lives in :mod:`kyber.commands` so every Kyber surface
(Discord, Telegram, WhatsApp, dashboard chat, CLI REPL, TUI) runs the
same set of commands. This module keeps the old CLI-facing function
names (``dispatch_slash``, ``REPLContext``, ``SlashResult``,
``available_command_names``, ``list_commands``) as thin wrappers so the
REPL and TUI code doesn't have to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kyber.commands.registry import (
    Command,
    CommandContext,
    CommandResult,
    available_command_names,
    dispatch,
    list_commands,
)


# Re-export ``Command`` so existing imports keep working.
__all__ = [
    "Command",
    "REPLContext",
    "SlashResult",
    "available_command_names",
    "dispatch_slash",
    "list_commands",
]


@dataclass
class REPLContext:
    """Legacy CLI-facing context shape.

    Mapped onto :class:`kyber.commands.CommandContext` in ``dispatch_slash``.
    Kept because the REPL and TUI both import it directly — removing it
    would break their import statements for no good reason.
    """

    session_id: str
    gateway_url: str
    gateway_token: str
    console: Any


# The TUI looks for ``should_exit`` and ``new_session_id`` fields; the
# gateway chat handler looks for ``reply_text`` and ``reset_session``.
# Same dataclass works everywhere.
SlashResult = CommandResult


async def dispatch_slash(raw: str, ctx: REPLContext, *, client=None) -> SlashResult:
    """Dispatch a CLI-side slash command through the shared registry.

    ``client`` is accepted for backward compatibility with older CLI
    code that passed an httpx client — not used anymore since the
    commands read config directly rather than round-tripping through
    the gateway.
    """
    channel_ctx = CommandContext(
        channel="cli",
        session_id=ctx.session_id,
        sender_id="cli",
        sender_name="cli",
        session_key=f"cli:{ctx.session_id}",
        http_client=client,
        supports_markdown=True,
    )
    result = await dispatch(raw, channel_ctx)
    if result is None:
        # Input wasn't actually a slash command — mirror the legacy
        # behaviour of returning an "unknown command" shaped response so
        # callers don't crash. This path shouldn't fire in practice since
        # callers check for "/" themselves, but it's cheap insurance.
        return CommandResult(
            reply_text="(that wasn't a slash command — send it as a regular message)"
        )
    return result
