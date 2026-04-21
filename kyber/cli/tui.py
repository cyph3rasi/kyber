"""Textual-based TUI for Kyber chat.

Run with ``kyber tui`` (or ``kyber chat --tui`` from Phase B.2).

This is the Charm-style "full app" chat client: a scrollable conversation
pane, a live status sidebar, a command input, and a footer of key bindings.
It talks to the existing gateway over ``POST /chat/turn`` — same endpoint
the dashboard chat and the plain REPL use — so every conversation shows up
in task history and every installed tool is available.

Slash commands are dispatched through :mod:`kyber.cli.slash_commands`, the
same registry the plain REPL uses. Anything registered there works here
too (and in Phase 1 will also work in Discord/Telegram/dashboard chat).

Scope notes:

* This is the core layout (Phase B.1). Streaming tool output, modal
  pickers, task panel, and command palette are follow-ups.
* Input is a single-line ``Input`` for now — Textual's ``TextArea`` gets
  layered in once we wire the custom key bindings for Enter vs Shift+Enter.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.theme import Theme
from textual.widgets import Footer, Header, Input, Label, Static

from kyber.cli.slash_commands import (
    Command,
    REPLContext,
    dispatch_slash,
    list_commands,
)
from kyber.config.loader import load_config


# Default theme: synthwave. Hot magenta + cyan on a deep purple night.
# Colors lifted from the popular "SynthWave '84" palette with tweaks for
# terminal contrast. Users will be able to pick another theme once we wire
# that into settings — for now this is the default look.
SYNTHWAVE_THEME = Theme(
    name="kyber-synthwave",
    primary="#ff7edb",       # hot magenta (assistant messages)
    secondary="#36f9f6",     # electric cyan (focus states, links)
    accent="#f97ec1",        # softer pink (titles, user tag)
    foreground="#f2f3f4",
    background="#1a1625",    # deep midnight purple
    surface="#241b2f",       # panel background
    panel="#2a2139",         # darker panel
    success="#72f1b8",       # mint green
    warning="#fede5d",       # laser yellow
    error="#fe4450",         # neon coral
    dark=True,
    variables={
        "border": "#ff7edb 30%",
        "scrollbar": "#ff7edb 40%",
    },
)


TUI_CSS = """
Screen {
    background: $background;
    layout: vertical;
}

Header {
    background: $panel;
    color: $accent;
}

Footer {
    background: $panel;
    color: $accent;
}

#main {
    height: 1fr;
    layout: horizontal;
}

#conversation {
    width: 1fr;
    padding: 1 2;
    background: $background;
    scrollbar-color: $primary 40%;
    scrollbar-color-hover: $primary 70%;
    scrollbar-color-active: $primary;
}

#sidebar {
    width: 34;
    padding: 1 2;
    background: $surface;
    height: 1fr;
}

.sidebar-title {
    color: $accent;
    text-style: bold;
    margin-top: 1;
}

.sidebar-value {
    color: $text;
    margin-bottom: 1;
}

.sidebar-muted {
    color: $text-muted;
}

.message-user {
    color: $accent;
    text-style: bold;
    margin-top: 1;
}

.message-kyber {
    color: $primary;
    text-style: bold;
    margin-top: 1;
}

.message-system {
    color: $warning;
    text-style: italic;
    margin-top: 1;
}

.message-body {
    margin-left: 2;
    margin-bottom: 1;
}

#input {
    height: 3;
    border: round $accent;
    margin: 0 1;
}

#input:focus {
    border: round $secondary;
}

#status-bar {
    display: none;
    height: auto;
    margin: 0 1;
    padding: 0 1;
    background: $surface;
    border: round $secondary 60%;
    color: $text;
}

#status-bar.-visible {
    display: block;
}

.status-line {
    color: $text;
}

.status-line-sub {
    color: $text-muted;
}

#slash-menu {
    display: none;
    height: auto;
    max-height: 10;
    margin: 0 1;
    padding: 0 1;
    background: $surface;
    border: round $primary 60%;
    color: $text;
}

#slash-menu.-visible {
    display: block;
}

.slash-row {
    padding: 0 1;
    height: 1;
    color: $text;
}

.slash-row.-selected {
    background: $accent 40%;
    color: $text;
    text-style: bold;
}

.slash-name {
    color: $secondary;
    text-style: bold;
}

.slash-desc {
    color: $text-muted;
}
"""


@dataclass
class Message:
    role: str  # "user" | "kyber" | "system"
    content: str
    timestamp: datetime


class ConversationPane(VerticalScroll):
    """Scrollable list of rendered messages."""

    DEFAULT_CSS = ""

    def add_message(self, msg: Message) -> None:
        role_classes = {
            "user": "message-user",
            "kyber": "message-kyber",
            "system": "message-system",
        }
        role_labels = {
            "user": "you",
            "kyber": "kyber",
            "system": "system",
        }
        header = Static(
            f"{role_labels.get(msg.role, msg.role)} · "
            f"{msg.timestamp.strftime('%H:%M:%S')}",
            classes=role_classes.get(msg.role, "message-system"),
        )
        body_text = msg.content.strip() or "(empty)"
        # Render markdown for kyber; plain text for user/system so we don't
        # accidentally run markdown formatting on raw user input.
        if msg.role == "kyber":
            body = Static(Markdown(body_text), classes="message-body")
        else:
            body = Static(body_text, classes="message-body")
        self.mount(header)
        self.mount(body)
        self.scroll_end(animate=False)


class Sidebar(Vertical):
    """Right-hand status sidebar."""

    def compose(self) -> ComposeResult:
        yield Static("session", classes="sidebar-title")
        yield Static("default", id="sb-session", classes="sidebar-value")
        yield Static("model", classes="sidebar-title")
        yield Static("—", id="sb-model", classes="sidebar-value")
        yield Static("provider", classes="sidebar-title")
        yield Static("—", id="sb-provider", classes="sidebar-value")
        yield Static("gateway", classes="sidebar-title")
        yield Static("● connecting…", id="sb-gateway", classes="sidebar-value sidebar-muted")
        yield Static("last turn", classes="sidebar-title")
        yield Static("—", id="sb-usage", classes="sidebar-value sidebar-muted")

    def set_session(self, value: str) -> None:
        self.query_one("#sb-session", Static).update(value)

    def set_model(self, value: str) -> None:
        self.query_one("#sb-model", Static).update(value or "—")

    def set_provider(self, value: str) -> None:
        self.query_one("#sb-provider", Static).update(value or "—")

    def set_gateway(self, ok: bool, detail: str = "") -> None:
        mark = "[green]●[/green] connected" if ok else "[red]●[/red] offline"
        if detail:
            mark = f"{mark} [dim]{detail}[/dim]"
        w = self.query_one("#sb-gateway", Static)
        w.update(mark)

    def set_last_usage(self, tokens: int | None, seconds: float | None) -> None:
        if tokens is None and seconds is None:
            self.query_one("#sb-usage", Static).update("—")
            return
        bits: list[str] = []
        if tokens is not None:
            bits.append(f"{tokens} tok")
        if seconds is not None:
            bits.append(f"{seconds:.1f}s")
        self.query_one("#sb-usage", Static).update(" · ".join(bits))


_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class TaskStatusBar(Label):
    """Live progress panel shown while the agent is working.

    Shows a braille spinner plus the active task's ``current_action``
    (same string other channels surface), iteration counter, and the
    last completed tool action. Uses ``Label`` rather than ``Static``
    because Textual 8.x's Static keeps its rendered Visual in a private
    name-mangled attribute that's fragile to touch — Label's simpler
    content model plays nicely with our stream of partial updates.

    Two loops drive it: a spinner timer calling :meth:`advance_spinner`
    every ~100ms, and a poller worker calling :meth:`update_from_task`
    every ~500ms with the active task for the current session.
    """

    def __init__(self) -> None:
        super().__init__(" ", id="status-bar")
        self._visible = False
        self._spinner_idx = 0
        self._current_action = "thinking…"
        self._iteration = 0
        self._max_iteration = 0
        self._recent_actions: list[str] = []

    @property
    def visible(self) -> bool:
        return self._visible

    def show(self) -> None:
        if not self._visible:
            self._visible = True
            self.add_class("-visible")
        self._refresh_content()

    def hide(self) -> None:
        if self._visible:
            self._visible = False
            self.remove_class("-visible")
        self._current_action = "thinking…"
        self._iteration = 0
        self._max_iteration = 0
        self._recent_actions = []

    def advance_spinner(self) -> None:
        if not self._visible:
            return
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
        self._refresh_content()

    def update_from_task(self, task: dict | None) -> None:
        if task is None:
            return
        action = (task.get("current_action") or "").strip() or "thinking…"
        iteration = int(task.get("iteration") or 0)
        max_iteration = int(task.get("max_iterations") or 0)
        recent = task.get("actions_completed") or []
        self._current_action = action
        self._iteration = iteration
        self._max_iteration = max_iteration
        self._recent_actions = [str(a) for a in recent[-3:] if a]
        self._refresh_content()

    def _refresh_content(self) -> None:
        if not self.is_mounted:
            return
        frame = _SPINNER_FRAMES[self._spinner_idx]
        iter_tag = ""
        if self._max_iteration:
            iter_tag = f"  ({self._iteration}/{self._max_iteration})"
        elif self._iteration:
            iter_tag = f"  (step {self._iteration})"
        content = f"{frame}  running: {self._current_action}{iter_tag}"
        if self._recent_actions:
            items = " · ".join(self._recent_actions[-2:])
            content = f"{content}\n  {items}"
        self.update(content)


class SlashMenu(Vertical):
    """Floating list of slash commands shown above the input.

    Populated via :meth:`refresh` with the current filter text (the input
    value after the leading ``/``). Appears only while the user is typing
    a slash command; Up/Down move the highlight, Tab or Enter inserts the
    selection, Esc / clearing the input hides it again.

    The menu never takes focus — the Input keeps it — so arrow-key handling
    is done at the App level and dispatched here while the menu is visible.
    """

    def __init__(self) -> None:
        super().__init__(id="slash-menu")
        self._visible = False
        self._entries: list[Command] = []
        self._selected: int = 0

    def on_mount(self) -> None:
        self.refresh_menu("")

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def selected_command(self) -> Command | None:
        if not self._entries:
            return None
        idx = max(0, min(self._selected, len(self._entries) - 1))
        return self._entries[idx]

    def refresh_menu(self, filter_text: str) -> None:
        """Rebuild rows for the given filter (the text after '/')."""
        fragment = (filter_text or "").lower().strip()
        all_cmds = list_commands()
        if fragment:
            matches = [
                c for c in all_cmds if fragment in c.name.lower()
            ]
        else:
            matches = all_cmds
        # Move aliases (/quit, /exit) next to each other is already handled
        # by sort; nothing else to do.
        self._entries = matches
        self._selected = 0
        self._rebuild_children()

    def _rebuild_children(self) -> None:
        # Remove existing rows.
        for child in list(self.children):
            child.remove()
        if not self._entries:
            self.mount(
                Static(
                    "[dim]no matching commands[/dim]",
                    classes="slash-row",
                )
            )
            return
        for idx, cmd in enumerate(self._entries):
            row_text = self._format_row(cmd)
            static = Static(row_text, classes="slash-row")
            if idx == self._selected:
                static.add_class("-selected")
            self.mount(static)

    def _format_row(self, cmd: Command) -> str:
        label = f"/{cmd.name}"
        if cmd.usage:
            label += f" {cmd.usage}"
        return f"[bold $secondary]{label}[/]  [dim]{cmd.summary}[/dim]"

    def show(self) -> None:
        if not self._visible:
            self._visible = True
            self.add_class("-visible")

    def hide(self) -> None:
        if self._visible:
            self._visible = False
            self.remove_class("-visible")

    def move(self, delta: int) -> None:
        if not self._entries:
            return
        self._selected = (self._selected + delta) % len(self._entries)
        # Rebuild to update the -selected class. Cheap — at most a dozen rows.
        self._rebuild_children()


class KyberTUI(App):
    """Kyber chat, Charm-style."""

    CSS = TUI_CSS
    TITLE = "kyber"
    SUB_TITLE = "chat"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+n", "new_session", "New session"),
        Binding("ctrl+l", "clear_view", "Clear view"),
        Binding("ctrl+k", "focus_input", "Focus input", show=False),
    ]

    session_id: reactive[str] = reactive("default")
    in_flight: reactive[bool] = reactive(False)

    def __init__(self, session_id: str = "default") -> None:
        super().__init__()
        self.initial_session_id = session_id
        self.session_id = session_id
        self._client: httpx.AsyncClient | None = None
        self._gateway_url: str = ""
        self._gateway_token: str = ""
        self._model: str = ""
        self._provider: str = ""

    # ── Layout ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield ConversationPane(id="conversation")
            yield Sidebar(id="sidebar")
        yield TaskStatusBar()
        yield SlashMenu()
        yield Input(placeholder="  type a message  (/ for commands)", id="input")
        yield Footer()

    # ── Lifecycle ──────────────────────────────────────────────────

    async def on_mount(self) -> None:
        # Register + activate the synthwave theme before anything else so the
        # initial paint uses the right colors instead of flashing the default.
        self.register_theme(SYNTHWAVE_THEME)
        self.theme = "kyber-synthwave"

        cfg = load_config()
        self._gateway_url = f"http://127.0.0.1:{cfg.gateway.port}"
        self._gateway_token = (cfg.dashboard.auth_token or "").strip()
        self._model = cfg.get_model()
        details = cfg.get_provider_details()
        self._provider = str(
            details.get("configured_provider_name") or details.get("provider_name") or ""
        )

        sidebar = self.query_one(Sidebar)
        sidebar.set_session(self.session_id)
        sidebar.set_model(self._model)
        sidebar.set_provider(self._provider)

        conv = self.query_one(ConversationPane)
        conv.add_message(
            Message(
                role="system",
                content=(
                    f"kyber chat · session **{self.session_id}** · "
                    f"model **{self._model}**\n\n"
                    "Enter to send · `/help` for commands · Ctrl-Q to quit"
                ),
                timestamp=datetime.now(),
            )
        )

        self._client = httpx.AsyncClient()
        self.query_one(Input).focus()
        self.probe_gateway()

        # Drive the status-bar spinner locally so it keeps animating even
        # between gateway polls. ~100ms feels alive without being twitchy.
        self.set_interval(0.1, self._tick_spinner)

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Actions ────────────────────────────────────────────────────

    async def action_new_session(self) -> None:
        if self._client is None:
            return
        headers = (
            {"Authorization": f"Bearer {self._gateway_token}"}
            if self._gateway_token
            else {}
        )
        try:
            await self._client.post(
                f"{self._gateway_url.rstrip('/')}/chat/reset",
                headers=headers,
                json={"session_id": self.session_id},
                timeout=15.0,
            )
        except httpx.HTTPError as e:
            self._add_system(f"reset failed: {e}")
            return
        self._add_system(f"session **{self.session_id}** cleared")

    def action_clear_view(self) -> None:
        conv = self.query_one(ConversationPane)
        # Remove every child widget; the scroll container itself stays.
        for child in list(conv.children):
            child.remove()

    def action_focus_input(self) -> None:
        self.query_one(Input).focus()

    # ── Input handling ─────────────────────────────────────────────

    # ── Slash menu (type "/" to browse commands) ──────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        menu = self.query_one(SlashMenu)
        value = event.value or ""
        if value.startswith("/") and "\n" not in value and " " not in value:
            menu.refresh_menu(value[1:])
            menu.show()
        else:
            menu.hide()

    async def on_key(self, event) -> None:
        """Intercept Up/Down/Tab/Esc while the slash menu is open.

        Listening on the App guarantees we see the key before the Input
        widget handles it, so we can navigate and auto-complete without
        fighting Input's default behavior.
        """
        menu = self.query_one(SlashMenu)
        if not menu.visible:
            return
        if event.key == "up":
            menu.move(-1)
            event.stop()
            event.prevent_default()
        elif event.key == "down":
            menu.move(1)
            event.stop()
            event.prevent_default()
        elif event.key == "tab":
            cmd = menu.selected_command
            if cmd is not None:
                inp = self.query_one("#input", Input)
                inp.value = f"/{cmd.name} "
                inp.cursor_position = len(inp.value)
                menu.hide()
            event.stop()
            event.prevent_default()
        elif event.key == "escape":
            menu.hide()
            event.stop()
            event.prevent_default()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        menu = self.query_one(SlashMenu)
        # If the menu is open and a command is selected, treat Enter as
        # "fill the command" rather than "send the message".
        if menu.visible and menu.selected_command is not None:
            raw = (event.value or "").strip()
            typed = raw[1:] if raw.startswith("/") else raw
            selected = menu.selected_command
            if not typed or selected.name != typed.lower():
                event.input.value = f"/{selected.name} "
                event.input.cursor_position = len(event.input.value)
                menu.hide()
                return

        menu.hide()
        value = (event.value or "").strip()
        event.input.value = ""
        if not value or self.in_flight:
            return

        if value.startswith("/"):
            await self._run_slash(value)
            return

        self._add_user(value)
        self.send_turn(value)

    async def _run_slash(self, raw: str) -> None:
        if self._client is None:
            return
        ctx = REPLContext(
            session_id=self.session_id,
            gateway_url=self._gateway_url,
            gateway_token=self._gateway_token,
            console=Console(),
        )
        result = await dispatch_slash(raw, ctx, client=self._client)
        if result.should_exit:
            self.exit()
            return
        if result.new_session_id:
            self.session_id = result.new_session_id
            self.query_one(Sidebar).set_session(self.session_id)
        if result.reply_text:
            self._add_system(result.reply_text)

    # ── Gateway I/O ────────────────────────────────────────────────

    @work(exclusive=False, group="gateway")
    async def probe_gateway(self) -> None:
        if self._client is None:
            return
        headers = (
            {"Authorization": f"Bearer {self._gateway_token}"}
            if self._gateway_token
            else {}
        )
        try:
            resp = await self._client.get(
                f"{self._gateway_url.rstrip('/')}/tasks",
                headers=headers,
                timeout=3.0,
            )
            ok = resp.status_code == 200
            detail = "" if ok else f"HTTP {resp.status_code}"
        except httpx.HTTPError as e:
            ok = False
            detail = type(e).__name__
        self.query_one(Sidebar).set_gateway(ok, detail)

    @work(exclusive=False, group="turn")
    async def send_turn(self, message: str) -> None:
        if self._client is None:
            return
        self.in_flight = True
        self._set_status_indicator(thinking=True)
        self._status_bar_show()
        # Kick off the polling worker. It runs alongside the turn request
        # and stops itself when in_flight flips to False.
        self.poll_active_task()
        started = datetime.now()

        headers = (
            {"Authorization": f"Bearer {self._gateway_token}"}
            if self._gateway_token
            else {}
        )
        try:
            resp = await self._client.post(
                f"{self._gateway_url.rstrip('/')}/chat/turn",
                headers=headers,
                json={"message": message, "session_id": self.session_id},
                timeout=300.0,
            )
        except httpx.HTTPError as e:
            self.in_flight = False
            self._set_status_indicator(thinking=False)
            self._status_bar_hide()
            self._add_system(f"gateway error: {e}")
            return

        elapsed = (datetime.now() - started).total_seconds()
        self.in_flight = False
        self._set_status_indicator(thinking=False)
        self._status_bar_hide()

        if resp.status_code == 401:
            self._add_system(
                "gateway rejected auth. run `kyber show-dashboard-token` and "
                "check ~/.kyber/.env."
            )
            return
        if resp.status_code != 200:
            snippet = (resp.text or "")[:300]
            self._add_system(f"gateway HTTP {resp.status_code}: {snippet}")
            return

        try:
            data = resp.json()
        except ValueError:
            self._add_system("gateway returned non-JSON response")
            return

        response_text = str(data.get("response") or "")
        self._add_kyber(response_text)
        self.query_one(Sidebar).set_last_usage(None, elapsed)

    @work(exclusive=True, group="task-poll")
    async def poll_active_task(self) -> None:
        """Poll /tasks while a turn is in flight, piping progress to the bar.

        Exclusive group so only one poller runs at a time. Exits on its own
        when ``self.in_flight`` clears.
        """
        if self._client is None:
            return
        headers = (
            {"Authorization": f"Bearer {self._gateway_token}"}
            if self._gateway_token
            else {}
        )
        status_bar = self.query_one(TaskStatusBar)
        while self.in_flight:
            try:
                resp = await self._client.get(
                    f"{self._gateway_url.rstrip('/')}/tasks",
                    headers=headers,
                    timeout=3.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    active = data.get("active") or []
                    task = self._pick_session_task(active)
                    status_bar.update_from_task(task)
            except httpx.HTTPError:
                # Transient errors are fine — keep the bar as-is and retry.
                pass
            await asyncio.sleep(0.5)

    def _pick_session_task(self, active_tasks: list[dict]) -> dict | None:
        """Pick the task record that corresponds to this TUI's session.

        The gateway's ``/chat/turn`` marks origin as channel=dashboard with
        the chat_id set to the session id. If nothing matches exactly, fall
        back to the most recent active task.
        """
        if not active_tasks:
            return None
        session_match = [
            t for t in active_tasks
            if t.get("origin_chat_id") == self.session_id
        ]
        candidates = session_match or active_tasks
        # Latest-started first so tool chains feel responsive even when
        # started_at is missing we still return a sensible pick.
        def _key(t: dict) -> str:
            return str(t.get("started_at") or t.get("created_at") or "")

        return sorted(candidates, key=_key, reverse=True)[0]

    # ── Helpers ────────────────────────────────────────────────────

    def _tick_spinner(self) -> None:
        self.query_one(TaskStatusBar).advance_spinner()

    def _status_bar_show(self) -> None:
        self.query_one(TaskStatusBar).show()

    def _status_bar_hide(self) -> None:
        self.query_one(TaskStatusBar).hide()

    def _set_status_indicator(self, thinking: bool) -> None:
        self.sub_title = "thinking…" if thinking else "chat"

    def _add_user(self, content: str) -> None:
        self.query_one(ConversationPane).add_message(
            Message(role="user", content=content, timestamp=datetime.now())
        )

    def _add_kyber(self, content: str) -> None:
        self.query_one(ConversationPane).add_message(
            Message(role="kyber", content=content, timestamp=datetime.now())
        )

    def _add_system(self, content: str) -> None:
        self.query_one(ConversationPane).add_message(
            Message(role="system", content=content, timestamp=datetime.now())
        )


def run_tui(session_id: str = "default") -> int:
    """Entry point for ``kyber tui``."""
    app = KyberTUI(session_id=session_id)
    app.run()
    return 0
