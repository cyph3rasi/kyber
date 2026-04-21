"""Interactive chat REPL for Kyber.

Exposed as ``kyber chat``. Connects to the running gateway (the same process
Discord/Telegram/dashboard talk to) via ``POST /chat/turn``, so conversations
appear in the usual task history and benefit from every installed tool.

Uses prompt_toolkit for:

* Multi-line editing (``Enter`` submits, ``Meta+Enter`` / ``Esc`` then ``Enter``
  inserts a newline — matching Claude Code / Hermes muscle memory).
* Persistent history at ``~/.kyber/chat_history``.
* Tab completion for slash commands (``/``).
* Clean Ctrl-C / Ctrl-D handling — Ctrl-C clears the current draft, Ctrl-D
  exits.

Slash commands are registered in :mod:`kyber.cli.slash_commands`. This file
wires the dispatch but doesn't own the handlers, so Phase 1 can swap in a
shared registry used by every channel without touching the REPL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from kyber.cli.slash_commands import (
    REPLContext,
    SlashResult,
    available_command_names,
    dispatch_slash,
)
from kyber.config.loader import load_config

logger = logging.getLogger(__name__)

HISTORY_PATH = Path.home() / ".kyber" / "chat_history"
DEFAULT_TIMEOUT_SECONDS = 300.0


class SlashCommandCompleter(Completer):
    """Tab-complete slash commands at the start of the line."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # Only trigger when the input is the first token and starts with "/".
        if not text.startswith("/"):
            return
        if "\n" in text or " " in text:
            return
        fragment = text[1:]
        for name in available_command_names():
            if name.startswith(fragment):
                yield Completion("/" + name, start_position=-len(text))


def _build_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("enter")
    def _submit_on_enter(event) -> None:
        """Enter submits; Meta+Enter inserts a literal newline."""
        buf = event.current_buffer
        if buf.complete_state:
            # If the completion menu is open, accept the selection instead.
            buf.complete_state = None
            return
        text = buf.text
        if not text.strip():
            # Empty input — do nothing.
            return
        buf.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event) -> None:
        event.current_buffer.insert_text("\n")

    # Allow Ctrl-J as an alternative newline for terminals where Meta+Enter
    # doesn't get through cleanly.
    @kb.add("c-j")
    def _ctrl_j_newline(event) -> None:
        event.current_buffer.insert_text("\n")

    return kb


def _prompt_style() -> Style:
    return Style.from_dict(
        {
            "prompt": "ansiblue bold",
            "continuation": "ansiblue",
        }
    )


def _continuation_prompt(width, line_number, wrap_count):
    # Dim grey dots aligned with the "kyber>" prompt.
    return HTML("<continuation>… </continuation>")


async def _post_turn(
    client: httpx.AsyncClient,
    gateway_url: str,
    token: str,
    session_id: str,
    message: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = await client.post(
        f"{gateway_url.rstrip('/')}/chat/turn",
        headers=headers,
        json={"message": message, "session_id": session_id},
        timeout=timeout,
    )
    if resp.status_code == 401:
        raise RuntimeError(
            "Gateway rejected auth token. Check ~/.kyber/.env matches "
            "the dashboard token (run `kyber show-dashboard-token`)."
        )
    if resp.status_code != 200:
        snippet = resp.text[:300] if resp.text else ""
        raise RuntimeError(f"Gateway error HTTP {resp.status_code}: {snippet}")
    data = resp.json()
    return str(data.get("response") or "")


async def _post_reset(
    client: httpx.AsyncClient,
    gateway_url: str,
    token: str,
    session_id: str,
) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = await client.post(
        f"{gateway_url.rstrip('/')}/chat/reset",
        headers=headers,
        json={"session_id": session_id},
        timeout=15.0,
    )
    if resp.status_code >= 400:
        snippet = resp.text[:200] if resp.text else ""
        raise RuntimeError(f"Reset failed HTTP {resp.status_code}: {snippet}")


async def _probe_gateway(gateway_url: str, token: str, timeout: float = 3.0) -> bool:
    """Return True if the gateway is reachable and auth token is accepted."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{gateway_url.rstrip('/')}/tasks", headers=headers)
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def _run_repl(session_id: str) -> int:
    console = Console()
    config = load_config()
    gateway_port = config.gateway.port
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    token = (config.dashboard.auth_token or "").strip()

    if not await _probe_gateway(gateway_url, token):
        console.print(
            Panel.fit(
                "[yellow]Could not reach the Kyber gateway at "
                f"{gateway_url}[/yellow]\n"
                "Start it with [cyan]kyber gateway[/cyan] or enable the "
                "background service via [cyan]kyber service install[/cyan].",
                title="Gateway not reachable",
                border_style="yellow",
            )
        )
        return 1

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ptk_session = PromptSession(
        history=FileHistory(str(HISTORY_PATH)),
        multiline=True,
        key_bindings=_build_key_bindings(),
        style=_prompt_style(),
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        prompt_continuation=_continuation_prompt,
    )

    ctx = REPLContext(
        session_id=session_id,
        gateway_url=gateway_url,
        gateway_token=token,
        console=console,
    )

    console.print(
        Panel.fit(
            f"[bold]Kyber chat[/bold]  [dim]session: {session_id}[/dim]\n"
            "[dim]Enter to send · Esc then Enter for newline · /help for commands · Ctrl-D to exit[/dim]",
            border_style="cyan",
        )
    )

    async with httpx.AsyncClient() as client:
        while True:
            try:
                with patch_stdout():
                    user_input = await ptk_session.prompt_async(
                        HTML("<prompt>kyber&gt; </prompt>")
                    )
            except KeyboardInterrupt:
                # Ctrl-C: clear current draft and show new prompt.
                continue
            except EOFError:
                # Ctrl-D: exit cleanly.
                console.print("[dim]Goodbye.[/dim]")
                return 0

            message = (user_input or "").strip()
            if not message:
                continue

            if message.startswith("/"):
                result = await dispatch_slash(message, ctx, client=client)
                if result.should_exit:
                    return 0
                if result.reply_text:
                    console.print(result.reply_text)
                # Apply side-effects (e.g. session switch).
                if result.new_session_id:
                    ctx.session_id = result.new_session_id
                    console.print(f"[dim]session → {ctx.session_id}[/dim]")
                continue

            # Normal message — send to gateway.
            with console.status("[dim]thinking...[/dim]", spinner="dots"):
                try:
                    response = await _post_turn(
                        client, gateway_url, token, ctx.session_id, message
                    )
                except asyncio.CancelledError:
                    console.print("[yellow](request cancelled)[/yellow]")
                    continue
                except Exception as e:
                    console.print(f"[red]error:[/red] {e}")
                    continue

            if response.strip():
                console.print(Markdown(response))
            else:
                console.print("[dim](no response)[/dim]")


def run_chat(session_id: str = "default") -> int:
    """Entry point for the ``kyber chat`` CLI command."""
    # Make Ctrl-C feel right inside the asyncio loop.
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except (ValueError, OSError):
        pass
    try:
        return asyncio.run(_run_repl(session_id))
    except KeyboardInterrupt:
        return 130
