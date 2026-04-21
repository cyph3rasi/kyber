"""CLI commands for kyber."""

import asyncio
import errno
import os
import platform
import socket
from contextlib import suppress
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from kyber import __version__, __logo__

app = typer.Typer(
    name="kyber",
    help=f"{__logo__} kyber - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} kyber v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """kyber - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize kyber configuration and workspace."""
    from kyber.config.loader import get_config_path, get_env_path, save_config
    from kyber.config.schema import Config
    from kyber.utils.helpers import get_workspace_path

    config_path = get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        if not typer.confirm("Overwrite?"):
            raise typer.Exit()

    # Create default config
    config = Config()
    save_config(config)
    console.print(f"[green]✓[/green] Created config at {config_path}")
    console.print(f"[green]✓[/green] Created secrets file at {get_env_path()} (mode 600)")

    # Create workspace
    workspace = get_workspace_path()
    console.print(f"[green]✓[/green] Created workspace at {workspace}")

    # Create default bootstrap files
    _create_workspace_templates(workspace)

    console.print(f"\n{__logo__} kyber is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.kyber/.env[/cyan]")
    console.print("     Example: KYBER_PROVIDERS__OPENROUTER__API_KEY=sk-or-v1-xxx")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print("  2. Chat: [cyan]kyber agent -m \"Hello!\"[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/kyber#-chat-apps[/dim]")


@app.command("migrate-secrets")
def migrate_secrets_cmd():
    """Migrate API keys from config.json to ~/.kyber/.env (secure storage)."""
    from kyber.config.loader import migrate_secrets, get_config_path, get_env_path, _config_has_secrets

    config_path = get_config_path()
    if not config_path.exists():
        console.print("[red]No config.json found. Run [cyan]kyber onboard[/cyan] first.[/red]")
        raise typer.Exit(1)

    if not _config_has_secrets(config_path):
        console.print("[green]✓[/green] config.json is already clean — no secrets to migrate.")
        env_path = get_env_path()
        if env_path.exists():
            console.print(f"  Secrets are in [cyan]{env_path}[/cyan]")
        raise typer.Exit()

    result = migrate_secrets(config_path)
    migrated = result.get("migrated", 0)
    console.print(f"[green]✓[/green] Migrated {migrated} secret(s) to [cyan]{get_env_path()}[/cyan]")
    console.print(f"  config.json has been scrubbed — no more plaintext keys.")
    console.print(f"  .env file permissions set to [cyan]600[/cyan] (owner only).")


@app.command("codex-models")
def codex_models_cmd(
    format: str = typer.Option("text", "--format", "-f", help="Output format: text | json"),
):
    """Print available OpenAI Codex models for the current ChatGPT login.

    Used by the installer and dashboard to populate the model picker when
    the user has selected the ChatGPT subscription provider. Reads tokens
    from ~/.codex/auth.json and refreshes them if needed.
    """
    import asyncio
    import json as _json

    from kyber.providers.codex_auth import CodexAuthError, find_codex_auth
    from kyber.providers.codex_provider import fetch_available_models

    if find_codex_auth() is None:
        console.print(
            "[red]No Codex login found at ~/.codex/auth.json. "
            "Run `codex login` first.[/red]"
        )
        raise typer.Exit(1)

    try:
        models = asyncio.run(fetch_available_models())
    except CodexAuthError as e:
        console.print(f"[red]Codex auth error: {e}[/red]")
        raise typer.Exit(1)

    if format == "json":
        typer.echo(_json.dumps(models))
    else:
        for m in models:
            typer.echo(m)


@app.command("chat")
def chat_cmd(
    session: str = typer.Option(
        "default",
        "--session",
        "-s",
        help="Session id for this conversation. Each id gets its own history.",
    ),
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Use the minimal prompt_toolkit REPL instead of the full TUI.",
    ),
):
    """Open an interactive chat with your Kyber agent.

    Default mode is the full Textual TUI — conversation pane, live status
    sidebar, footer key hints. Pass ``--plain`` for the minimal REPL.

    Talks to the running gateway (same one Discord/Telegram/dashboard use),
    so conversations appear in the usual task history.
    """
    if plain:
        from kyber.cli.chat_repl import run_chat

        rc = run_chat(session_id=session)
        raise typer.Exit(rc)

    from kyber.cli.tui import run_tui

    rc = run_tui(session_id=session)
    raise typer.Exit(rc)


@app.command("tui")
def tui_cmd(
    session: str = typer.Option(
        "default",
        "--session",
        "-s",
        help="Session id for this conversation.",
    ),
):
    """Launch the full Textual TUI (alias for `kyber chat`)."""
    from kyber.cli.tui import run_tui

    rc = run_tui(session_id=session)
    raise typer.Exit(rc)


@app.command("show-dashboard-token")
def show_dashboard_token():
    """Print the dashboard auth token."""
    from kyber.config.loader import load_config

    config = load_config()
    token = config.dashboard.auth_token.strip()
    if not token:
        console.print("[yellow]No dashboard token set. Start the dashboard to generate one:[/yellow]")
        console.print("  kyber dashboard")
        raise typer.Exit(1)

    console.print(token)


@app.command("dashboard-info")
def dashboard_info(
    open_browser: bool = typer.Option(
        False,
        "--open",
        "-o",
        help="Open the one-click login URL in your default browser.",
    ),
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Print a single URL only (useful for scripts / `$(kyber dashboard-info --plain)`).",
    ),
):
    """Show dashboard status, token, and a one-click login URL.

    Probes the configured dashboard port to report whether it's up, prints
    the auth token, and produces a ``http://host:port/?token=...`` link
    that signs you in automatically when clicked.
    """
    import urllib.parse
    import urllib.request
    import webbrowser

    from kyber.config.loader import load_config
    from rich.panel import Panel
    from rich.table import Table

    config = load_config()
    host = (config.dashboard.host or "127.0.0.1").strip()
    # Dashboards bound to 0.0.0.0 / :: listen everywhere but are opened
    # locally — point users at a browsable address.
    display_host = (
        "127.0.0.1" if host in ("0.0.0.0", "", "::", "::0") else host
    )
    port = int(config.dashboard.port or 18890)
    token = (config.dashboard.auth_token or "").strip()

    base_url = f"http://{display_host}:{port}"
    login_url = (
        f"{base_url}/?token={urllib.parse.quote(token, safe='')}"
        if token
        else base_url
    )

    # Probe the dashboard. GET / is public (serves index.html).
    status_ok = False
    status_detail = ""
    try:
        req = urllib.request.Request(base_url, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:  # noqa: S310
            status_ok = 200 <= resp.status < 400
            status_detail = f"HTTP {resp.status}"
    except Exception as e:
        status_detail = type(e).__name__

    # Service mode state (best-effort).
    service_state = ""
    try:
        from kyber.service import service_status

        info = service_status()
        dash_unit = info.dashboard
        if dash_unit.installed and dash_unit.active:
            service_state = f"{info.backend} · active"
        elif dash_unit.installed:
            service_state = f"{info.backend} · installed (inactive)"
        else:
            service_state = "manual (no service installed)"
    except Exception:
        service_state = "unknown"

    if plain:
        console.print(login_url if token else base_url)
    else:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", justify="right")
        table.add_column()
        status_line = (
            "[green]● running[/green]" if status_ok
            else f"[yellow]● not reachable[/yellow] [dim]({status_detail})[/dim]"
        )
        table.add_row("status", status_line)
        table.add_row("mode", service_state)
        table.add_row("url", f"[cyan]{base_url}[/cyan]")
        table.add_row("token", token or "[dim](none)[/dim]")
        if token:
            table.add_row("login url", f"[link={login_url}][cyan]{login_url}[/cyan][/link]")
            table.add_row("", "[dim]click the link above — you'll land signed in[/dim]")
        console.print(Panel(table, title="Kyber Dashboard", border_style="magenta", expand=False))

        if not status_ok:
            console.print(
                "[dim]The dashboard doesn't appear to be running. Start it with:[/dim]"
            )
            console.print("  [cyan]kyber service install[/cyan]   (run as background service)")
            console.print("  [cyan]kyber dashboard[/cyan]          (run in this terminal)")

    if open_browser:
        if not token:
            console.print("[yellow]No token available; opening bare URL.[/yellow]")
        webbrowser.open(login_url)


def _create_workspace_templates(workspace: Path):
    """Create default workspace template files."""
    templates = {
        "AGENTS.md": """# Agent Instructions

This file is for your custom instructions and preferences. Edit it to customize how kyber behaves for you.
System instructions (tools, cron, heartbeat, guidelines) are built into kyber and update automatically with upgrades.

## Custom Instructions

Add any project-specific or personal instructions here. For example:
- Preferred coding style or languages
- Project context the agent should know about
- Custom workflows or conventions
""",
        "SOUL.md": """# Soul

I am kyber, a lightweight AI assistant.

## Personality

- Helpful and friendly
- Concise and to the point
- Curious and eager to learn

## Values

- Accuracy over speed
- User privacy and safety
- Transparency in actions
""",
        "USER.md": """# User

Information about the user goes here.

## Preferences

- Communication style: (casual/formal)
- Timezone: (your timezone)
- Language: (your preferred language)
""",
        "IDENTITY.md": """# Identity

Use this file for stable identity constraints that should always apply.
Examples:
- Role and scope boundaries
- Non-negotiable behavioral rules
- Product-specific identity details
""",
        "TOOLS.md": """# Tools

Optional tool usage guidance for this workspace.
Examples:
- Preferred command patterns
- Safe/unsafe environments
- Project-specific tool conventions
""",
        "HEARTBEAT.md": """# Heartbeat Tasks

This file is checked on every heartbeat interval.
Add recurring checks or maintenance tasks as checklist items.

## Active Tasks

- [ ] (add a recurring task here)
""",
    }
    
    for filename, content in templates.items():
        file_path = workspace / filename
        if not file_path.exists():
            file_path.write_text(content)
            console.print(f"  [dim]Created {filename}[/dim]")
    
    # Create memory directory and MEMORY.md
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        memory_file.write_text("""# Long-term Memory

This file stores important information that should persist across sessions.

## User Information

(Important facts about the user)

## Preferences

(User preferences learned over time)

## Important Notes

(Things to remember)
""")
        console.print("  [dim]Created memory/MEMORY.md[/dim]")

    # Create skills directory scaffold
    skills_dir = workspace / "skills"
    skills_dir.mkdir(exist_ok=True)
    skills_readme = skills_dir / "README.md"
    canonical_skills_readme = (
        "# Skills\n\n"
        "Drop custom skills here as folders containing a `SKILL.md`.\n\n"
        "Example:\n"
        "- `skills/my-skill/SKILL.md`\n\n"
        "This workspace is the canonical skills location.\n"
    )
    if not skills_readme.exists():
        skills_readme.write_text(canonical_skills_readme)
        console.print("  [dim]Created skills/README.md[/dim]")
    else:
        existing = skills_readme.read_text()
        legacy_markers = (
            "Kyber also supports managed skills in `~/.kyber/skills/<skill>/SKILL.md`.",
            "Kyber also supports managed skills in `~/.kyber/skills`.",
        )
        if any(marker in existing for marker in legacy_markers):
            skills_readme.write_text(canonical_skills_readme)
            console.print("  [dim]Updated skills/README.md to canonical workspace-only text[/dim]")


# ============================================================================
# Shared Agent Factory (Hermes-style direct tool-calling)
# ============================================================================


def _create_agent(
    config,
    bus,
    task_history_path: Path | None = None,
    channels = None,
):
    """Create an AgentCore with providers from config.

    Uses the new hermes-style direct tool-calling engine.
    Shared by the ``gateway`` and ``agent`` commands.
    """
    from kyber.providers.openai_provider import OpenAIProvider
    from kyber.agent.core import AgentCore

    # ── Provider ──
    details = config.get_provider_details()
    api_key = details.get("api_key")
    api_base = details.get("api_base")
    provider_name = details.get("provider_name") or config.get_provider_name()
    model = config.get_model()

    provider_name_str = str(provider_name or "")
    provider_name_lc = provider_name_str.lower()

    # Subscription providers (ChatGPT via Codex OAuth, Claude via Claude
    # Code OAuth) take a different code path — they read tokens from the
    # official CLI's credential store and talk to a non-OpenAI-compatible
    # API shape. One branch per subscription kind.
    _subscription_active = False
    if details.get("is_subscription"):
        kind = str(details.get("subscription_kind") or "")
        if kind == "chatgpt":
            from kyber.providers.codex_auth import CodexAuthError, find_codex_auth
            from kyber.providers.codex_provider import CodexProvider

            if find_codex_auth() is None:
                console.print(
                    "[red]ChatGPT subscription provider is selected but no "
                    "Codex login was found at ~/.codex/auth.json.[/red]"
                )
                console.print(
                    "[yellow]Run `codex login` (install with "
                    "`npm i -g @openai/codex` if needed), then start "
                    "Kyber again.[/yellow]"
                )
                raise typer.Exit(1)

            try:
                provider = CodexProvider(default_model=model)
            except CodexAuthError as e:
                console.print(f"[red]Codex auth error: {e}[/red]")
                if e.relogin_required:
                    console.print(
                        "[yellow]Run `codex login` to re-authenticate.[/yellow]"
                    )
                raise typer.Exit(1)

        elif kind == "claude":
            # Removed in 2026.4.21.53. Reusing Claude Code's OAuth token
            # from third-party clients has been getting accounts banned,
            # so Kyber no longer ships this path. Print a loud migration
            # message and refuse to start — safer than silently routing
            # traffic that might bonk the user's subscription.
            console.print(
                "[red]Claude subscription provider was removed for safety.[/red]"
            )
            console.print(
                "[yellow]Anthropic has been banning accounts that use the "
                "Claude Code OAuth token from non-CLI clients.\n"
                "Switch to ChatGPT/Codex, an OpenRouter key, or a direct "
                "Anthropic API key in the dashboard or config.json.[/yellow]"
            )
            raise typer.Exit(1)

        else:
            console.print(
                f"[red]Unknown subscription kind {kind!r}; cannot build "
                "provider.[/red]"
            )
            raise typer.Exit(1)

        _subscription_active = True

    if not _subscription_active:
        # Normalize accidental "Bearer <key>" pastes. SDK adds "Bearer" itself.
        if isinstance(api_key, str):
            token = api_key.strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            api_key = token

        # Custom-provider key fallback from env (useful for manual env setup).
        if details.get("is_custom", False) and not api_key:
            from kyber.config.loader import custom_provider_env_key

            env_key_name = custom_provider_env_key(provider_name_str) or ""
            api_key = (os.environ.get(env_key_name, "") or "").strip() if env_key_name else ""
            if not api_key and "minimax" in provider_name_lc:
                api_key = (os.environ.get("MINIMAX_API_KEY", "") or "").strip()

        if details.get("is_custom", False) and not api_key:
            console.print(f"[red]Error: Custom provider '{provider_name}' requires an API key.[/red]")
            raise typer.Exit(1)

        # Map provider name to OpenAIProvider's expected format
        provider_type = "openrouter"  # default
        if "openai" in provider_name_lc:
            provider_type = "openai"
        elif "anthropic" in provider_name_lc:
            provider_type = "anthropic"
        elif "z.ai" in provider_name_lc or details.get("is_custom"):
            # z.ai and other custom providers use OpenAI-compatible API
            provider_type = "openai"

        provider = OpenAIProvider(
            api_key=api_key,
            api_base=api_base,
            provider=provider_type,
            default_model=model,
            enable_prompt_cache=bool(
                getattr(config.agents.defaults, "enable_prompt_cache", True)
            ),
        )

    # ── Persona ──
    workspace = config.workspace_path
    soul_path = workspace / "SOUL.md"
    persona = ""
    if soul_path.exists():
        persona = soul_path.read_text(encoding="utf-8")

    # ── Progress callback for status updates ──
    async def _progress_callback(channel: str, chat_id: str, status_line: str, status_key: str = "") -> None:
        """Send tool execution progress to the appropriate channel."""
        if not channels:
            return
        channel_obj = channels.get_channel(channel)
        if not channel_obj:
            return
        # Discord has start/update/clear_status_message methods
        if hasattr(channel_obj, "update_status_message"):
            try:
                chat_id_int = int(chat_id)
                if status_line == "__KYBER_STATUS_START__" and hasattr(channel_obj, "start_status_message"):
                    try:
                        await channel_obj.start_status_message(chat_id_int, status_key)
                    except TypeError:
                        await channel_obj.start_status_message(chat_id_int)
                    return
                if status_line == "__KYBER_STATUS_END__" and hasattr(channel_obj, "clear_status_message"):
                    try:
                        await channel_obj.clear_status_message(chat_id_int, status_key)
                    except TypeError:
                        await channel_obj.clear_status_message(chat_id_int)
                    return
                try:
                    await channel_obj.update_status_message(chat_id_int, status_line, status_key)
                except TypeError:
                    await channel_obj.update_status_message(chat_id_int, status_line)
            except (ValueError, TypeError):
                pass

    # ── AgentCore (hermes-style) ──
    defaults = config.agents.defaults
    per_channel_policy: dict[str, dict[str, list[str]]] = {}
    try:
        raw_policy = getattr(config.tools, "per_channel", {}) or {}
        for ch_name, policy in raw_policy.items():
            per_channel_policy[str(ch_name).strip().lower()] = {
                "allow": list(getattr(policy, "allow", []) or []),
                "deny": list(getattr(policy, "deny", []) or []),
            }
    except Exception:
        # Malformed per-channel policy shouldn't block agent startup —
        # just fall through with an empty policy (full tool catalog).
        pass

    return AgentCore(
        bus=bus,
        provider=provider,
        workspace=workspace,
        persona_prompt=persona or None,
        model=model,
        task_history_path=task_history_path,
        timezone=defaults.timezone or None,
        progress_callback=_progress_callback if channels else None,
        tool_result_max_chars=int(getattr(defaults, "tool_result_max_chars", 20_000)),
        tool_result_keep_recent=int(getattr(defaults, "tool_result_keep_recent", 3)),
        history_summary_trigger=int(getattr(defaults, "history_summary_trigger", 30)),
        history_summary_keep_recent=int(getattr(defaults, "history_summary_keep_recent", 12)),
        per_channel_tool_policy=per_channel_policy,
    )


# Legacy alias for backwards compatibility
_create_orchestrator = _create_agent


# ============================================================================
# Gateway / Server
# ============================================================================


def _can_bind(host: str, port: int) -> tuple[bool, str]:
    """Check whether host:port can be bound without consuming it."""
    try:
        addrs = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
            flags=socket.AI_PASSIVE,
        )
    except socket.gaierror:
        return False, "invalid_host"

    in_use = False
    for family, socktype, proto, _, sockaddr in addrs:
        with socket.socket(family, socktype, proto) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(sockaddr)
                return True, "ok"
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    in_use = True
                continue

    if in_use:
        return False, "in_use"
    return False, "bind_failed"


def _suppress_websocket_deprecation_noise() -> None:
    """Suppress known deprecation noise from uvicorn websocket backends."""
    import warnings

    warnings.filterwarnings(
        "ignore",
        message=r"websockets\.legacy is deprecated.*",
        category=DeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r"websockets\.server\.WebSocketServerProtocol is deprecated",
        category=DeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r"parameter 'timeout' of type 'float' is deprecated, please use 'timeout=ClientWSTimeout\(ws_close=\.\.\.\)'",
        category=DeprecationWarning,
    )


def _select_uvicorn_ws_backend() -> str:
    """Use wsproto when available; otherwise fall back to websockets."""
    import importlib.util

    return "wsproto" if importlib.util.find_spec("wsproto") is not None else "websockets"


def _warn_if_openhands_runtime_unusable(console_obj=console) -> None:
    """Best-effort OpenHands runtime preflight.

    This must never terminate gateway startup; otherwise user services can
    enter restart loops due to a pre-existing ownership mismatch in
    ~/.openhands on WSL/VPS hosts.
    """
    from kyber.utils.openhands_runtime import ensure_openhands_runtime_dirs

    try:
        ensure_openhands_runtime_dirs()
    except Exception as e:
        console_obj.print(f"[yellow]⚠[/yellow] OpenHands runtime preflight failed: {e}")
        console_obj.print(
            "[yellow]Gateway will continue running, but OpenHands chat/tasks may fail until this is fixed.[/yellow]"
        )


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port (defaults to config.gateway.port)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the kyber gateway.

    Must run under a service manager (launchd on macOS, systemd --user on
    Linux). Running it standalone caused most of the port-conflict /
    orphan / restart-loop bugs, so Kyber now refuses to start this way.
    """
    from kyber.service import running_under_service_manager

    if not running_under_service_manager():
        console.print(
            "[red]kyber gateway must run under a service manager.[/red]\n"
            "Kyber enforces this because running the gateway in a bare shell was "
            "causing port conflicts, orphan processes, and restart loops.\n\n"
            "[bold]Set up (or repair) service mode:[/bold]\n"
            "  [cyan]kyber service install[/cyan]   # installs + starts both services\n"
            "  [cyan]kyber service status[/cyan]    # confirm they're healthy\n\n"
            "[dim]If you are a developer who genuinely wants a foreground "
            "process for debugging, set KYBER_FORCE_FOREGROUND=1 in the "
            "environment.[/dim]"
        )
        raise typer.Exit(2)

    from kyber.config.loader import load_config, get_data_dir
    from kyber.config.loader import save_config
    from kyber.bus.queue import MessageBus
    from kyber.channels.manager import ChannelManager
    from kyber.cron.service import CronService
    from kyber.cron.types import CronJob
    from kyber.cron.paths import get_cron_store_path
    from kyber.heartbeat.service import HeartbeatService
    from kyber.gateway.api import create_gateway_app
    
    _suppress_websocket_deprecation_noise()
    ws_backend = _select_uvicorn_ws_backend()

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)
    
    config = load_config()

    # Warn if config.json still has plaintext secrets
    from kyber.config.loader import _config_has_secrets
    if _config_has_secrets():
        console.print(
            "[yellow]⚠  config.json contains plaintext API keys. "
            "Run [cyan]kyber migrate-secrets[/cyan] to move them to ~/.kyber/.env[/yellow]"
        )

    # Default port comes from config so dashboard proxy and gateway stay in sync.
    if port is None:
        port = int(config.gateway.port)
    elif int(config.gateway.port) != int(port):
        config.gateway.port = int(port)
        save_config(config)

    bind_ok, bind_reason = _can_bind(config.gateway.host, port)
    if not bind_ok:
        if bind_reason == "in_use":
            console.print(
                f"[red]✗[/red] Could not start gateway API on "
                f"{config.gateway.host}:{port} (address already in use)."
            )
        elif bind_reason == "invalid_host":
            console.print(
                f"[red]✗[/red] Could not start gateway API on "
                f"{config.gateway.host}:{port} (invalid host)."
            )
        else:
            console.print(
                f"[red]✗[/red] Could not start gateway API on "
                f"{config.gateway.host}:{port}."
            )
        raise typer.Exit(1)

    console.print(f"{__logo__} Starting kyber gateway on port {port}...")

    # Ensure dashboard auth token exists (used for gateway task API auth).
    if not (config.dashboard.auth_token or "").strip():
        import secrets
        config.dashboard.auth_token = secrets.token_urlsafe(32)
        save_config(config)

    # Capture ERROR logs for the dashboard Debug tab.
    from kyber.logging.error_store import init_error_store
    init_error_store(get_data_dir() / "logs" / "gateway-errors.jsonl")

    # Ensure workspace scaffold exists even if user didn't run `kyber onboard`.
    workspace = config.workspace_path
    _create_workspace_templates(workspace)

    # Best-effort preflight: never block startup (prevents service bootloops).
    _warn_if_openhands_runtime_unusable()
    
    # Create components
    bus = MessageBus()
    
    # Create channel manager first (so agent can reference it for progress callbacks)
    channels = ChannelManager(config, bus)
    
    agent = _create_orchestrator(
        config,
        bus,
        task_history_path=get_data_dir() / "tasks" / "history.jsonl",
        channels=channels,
    )

    # Hand the agent down to every channel so the shared slash-command
    # dispatcher can reach it for /cancel, /usage, etc. No-op for
    # channels that are currently disabled.
    channels.attach_agent(agent)

    # Note: Discord typing indicators are handled directly in DiscordChannel class
    
    # Create cron service
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        # Special case: background ClamAV scan runs directly without the agent
        if "clamscan" in job.name.lower() or "clamav" in job.name.lower() or "clamscan" in job.payload.message.lower():
            import asyncio
            from kyber.security.clamscan import run_clamscan
            loop = asyncio.get_event_loop()
            report = await loop.run_in_executor(None, run_clamscan)
            status = report.get("status", "error")
            count = len(report.get("infected_files", []))
            if status == "clean":
                return "ClamAV scan complete — no threats detected."
            elif status == "threats_found":
                return f"ClamAV scan complete — {count} threat(s) detected! Check the Security Center."
            else:
                return f"ClamAV scan error: {report.get('error', 'unknown')}"

        from kyber.cron.runtime import resolve_job_context

        # Cron runs should preserve the same conversation context whenever
        # available, while still routing delivery to the requested channel.
        session_key, channel, chat_id = resolve_job_context(job)

        response = await agent.process_direct(
            job.payload.message,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
        )
        # If delivery is configured but the agent didn't spawn a background
        # task (i.e. it handled inline), push the response to the channel.
        # Background tasks handle their own completion delivery.
        if job.payload.deliver and job.payload.to and response:
            from kyber.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "discord",
                chat_id=job.payload.to,
                content=response or ""
            ))
        return response
    
    cron_store_path = get_cron_store_path()
    user_tz = config.agents.defaults.timezone or None
    cron = CronService(cron_store_path, on_job=on_cron_job, timezone=user_tz)

    # Auto-register daily ClamAV scan if ClamAV is installed
    _ensure_clamscan_cron(cron)

    # Create heartbeat service
    async def on_heartbeat(prompt: str) -> str:
        """Execute heartbeat through the agent."""
        return await agent.process_direct(
            prompt,
            session_key="internal:heartbeat",
            channel="internal",
            chat_id="heartbeat",
        )
    
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,  # 30 minutes
        enabled=True
    )
    
    # Channel manager was already created above and passed to the agent
    # channels = ChannelManager(config, bus)  # REMOVED: duplicate was breaking progress callbacks
    
    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")
    
    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")
    
    console.print(f"[green]✓[/green] Heartbeat: every 30m")

    async def run() -> int:
        api_server = None
        api_task: asyncio.Task | None = None
        agent_task: asyncio.Task | None = None
        channels_task: asyncio.Task | None = None
        try:
            # Start local gateway API for dashboard task management first.
            # If bind fails, avoid starting channels/cron/heartbeat.
            import uvicorn

            # Self-heal common port-already-in-use crash loop: if a previous
            # kyber gateway is still squatting the port (manual invocation,
            # slow-exiting service, etc.), kill it and wait for release.
            from kyber.service import ensure_port_free as _ensure_port_free

            free, msg = _ensure_port_free(port)
            if not free:
                console.print(
                    f"[red]✗[/red] Cannot start gateway: {msg}. "
                    "Free the port and retry."
                )
                return 1

            api_app = create_gateway_app(agent, config.dashboard.auth_token)
            api_config = uvicorn.Config(
                api_app,
                host=config.gateway.host,
                port=port,
                log_level="warning",
                access_log=False,
                ws=ws_backend,
            )
            api_server = uvicorn.Server(api_config)
            api_task = asyncio.create_task(api_server.serve())

            # Wait until the API either starts or fails.
            while not api_server.started and not api_task.done():
                await asyncio.sleep(0.05)
            if api_task.done():
                try:
                    await api_task
                except SystemExit:
                    pass
                console.print(
                    f"[red]✗[/red] Could not start gateway API on "
                    f"{config.gateway.host}:{port} (address already in use)."
                )
                return 1

            await cron.start()
            await heartbeat.start()

            # Kyber network: if this instance is configured as a spoke, keep
            # a live WebSocket to its host. Host-side routes are mounted
            # inside create_gateway_app, so no extra startup is needed there.
            spoke_client = None
            if (config.network.role or "").strip().lower() == "spoke":
                from kyber.network.spoke import get_spoke_client

                spoke_client = get_spoke_client()
                await spoke_client.start()

            agent_task = asyncio.create_task(agent.run(), name="agent")
            channels_task = asyncio.create_task(channels.start_all(), name="channels")
            # api_task is already created above as the uvicorn server.

            # All three should run until the process is signalled to stop.
            # If ANY of them ends (cleanly or with an error), the gateway is
            # no longer functional and we should exit non-zero so systemd's
            # Restart=on-failure policy brings us back. Without this guard a
            # benign early-return from e.g. channels.start_all() (no channels
            # configured) used to trigger a tight restart loop.
            try:
                done, pending = await asyncio.wait(
                    {agent_task, channels_task, api_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                first = next(iter(done))
                name = first.get_name() if hasattr(first, "get_name") else "task"
                exc = first.exception()
                if exc is not None:
                    console.print(
                        f"[red]✗[/red] Gateway subtask [yellow]{name}[/yellow] "
                        f"failed: {type(exc).__name__}: {exc}"
                    )
                else:
                    console.print(
                        f"[yellow]![/yellow] Gateway subtask [bold]{name}[/bold] "
                        "exited unexpectedly. Shutting down so the service "
                        "manager can restart us cleanly."
                    )
                for t in pending:
                    t.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await t
                return 1
            finally:
                if spoke_client is not None:
                    with suppress(Exception):
                        await spoke_client.stop()
        except KeyboardInterrupt:
            console.print("\nShutting down...")
            return 0
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
            if code != 0:
                console.print(
                    f"[red]✗[/red] Could not start gateway API on "
                    f"{config.gateway.host}:{port} (address already in use)."
                )
            return code
        finally:
            heartbeat.stop()
            cron.stop()
            agent.stop()
            if api_server is not None:
                api_server.should_exit = True

            with suppress(Exception):
                await channels.stop_all()

            for task in (agent_task, channels_task, api_task):
                if task is None:
                    continue
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError, SystemExit):
                    await task

    exit_code = asyncio.run(run())
    if exit_code:
        raise typer.Exit(exit_code)




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
):
    """Interact with the agent directly."""
    from kyber.config.loader import load_config
    from kyber.bus.queue import MessageBus
    
    config = load_config()
    _create_workspace_templates(config.workspace_path)
    bus = MessageBus()
    agent_instance = _create_orchestrator(config, bus)
    
    if message:
        # Single message mode
        async def run_once():
            response = await agent_instance.process_direct(message, session_id)
            console.print(f"\n{__logo__} {response}")
        
        asyncio.run(run_once())
    else:
        # Interactive mode
        console.print(f"{__logo__} Interactive mode (Ctrl+C to exit)\n")
        
        async def run_interactive():
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                    if not user_input.strip():
                        continue
                    
                    response = await agent_instance.process_direct(user_input, session_id)
                    console.print(f"\n{__logo__} {response}\n")
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break
        
        asyncio.run(run_interactive())




@app.command()
def dashboard(
    host: str | None = typer.Option(None, "--host", help="Dashboard host"),
    port: int | None = typer.Option(None, "--port", help="Dashboard port"),
    show_token: bool = typer.Option(False, "--show-token", help="Print dashboard token"),
):
    """Start the kyber web dashboard.

    Like ``kyber gateway``, this requires a service manager — manual
    foreground invocations caused port conflicts and orphans.
    """
    from kyber.service import running_under_service_manager

    if not running_under_service_manager():
        console.print(
            "[red]kyber dashboard must run under a service manager.[/red]\n\n"
            "[bold]Set up (or repair) service mode:[/bold]\n"
            "  [cyan]kyber service install[/cyan]   # installs + starts both services\n"
            "  [cyan]kyber dashboard-info[/cyan]    # then view the URL + token\n\n"
            "[dim]For foreground debugging set KYBER_FORCE_FOREGROUND=1.[/dim]"
        )
        raise typer.Exit(2)

    import secrets

    from kyber.config.loader import load_config, save_config
    from kyber.dashboard.server import create_dashboard_app
    from kyber.config.loader import get_data_dir
    from kyber.logging.error_store import init_error_store

    config = load_config()
    _create_workspace_templates(config.workspace_path)
    dash = config.dashboard

    _suppress_websocket_deprecation_noise()
    ws_backend = _select_uvicorn_ws_backend()
    import uvicorn

    if host:
        dash.host = host
    if port:
        dash.port = port

    if not dash.auth_token.strip():
        dash.auth_token = secrets.token_urlsafe(32)
        save_config(config)
        console.print("[green]✓[/green] Generated new dashboard token and saved to config")

    if dash.host not in {"127.0.0.1", "localhost", "::1"} and not dash.allowed_hosts:
        # Non-loopback binds (including the 0.0.0.0 default) are allowed but
        # noted. TrustedHostMiddleware drops its DNS-rebinding guard in this
        # mode — every endpoint is still bearer-token-protected, so the
        # dashboard isn't broadly exposed, just reachable by IP (e.g. over
        # Tailscale, LAN, or a VPS floating IP).
        console.print(
            f"[dim]Dashboard bound to {dash.host}:{dash.port} — reachable on "
            "all interfaces; auth enforced by bearer token.[/dim]"
        )
        console.print(
            "[dim]To restrict by Host header, set `dashboard.allowedHosts` "
            "in ~/.kyber/config.json.[/dim]"
        )

    # Self-heal port conflicts from stale kyber dashboards.
    from kyber.service import ensure_port_free as _ensure_port_free

    free, msg = _ensure_port_free(int(dash.port))
    if not free:
        console.print(f"[red]✗[/red] Cannot start dashboard: {msg}.")
        raise typer.Exit(1)

    app = create_dashboard_app(config)
    # Capture dashboard process errors too (separate file).
    init_error_store(get_data_dir() / "logs" / "dashboard-errors.jsonl")
    url = f"http://{dash.host}:{dash.port}"
    console.print(f"{__logo__} Kyber dashboard running at {url}")
    if show_token:
        console.print(f"  Token: [bold]{dash.auth_token}[/bold]")
    else:
        masked = dash.auth_token[:6] + "…" + dash.auth_token[-4:]
        console.print(f"  Token: [dim]{masked}[/dim]  (run with --show-token to reveal)")
    console.print(f"  Open:  {url}")
    # Dashboard is chatty when polling; keep access logs off by default.
    uvicorn.run(
        app,
        host=dash.host,
        port=dash.port,
        log_level="warning",
        access_log=False,
        ws=ws_backend,
    )


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")

skills_app = typer.Typer(help="Manage skills (skills.sh compatible)")
app.add_typer(skills_app, name="skills")


@skills_app.command("list")
def skills_list():
    """List skills from workspace and builtin skill sources."""
    from kyber.config.loader import load_config
    from kyber.agent.skills import SkillsLoader

    cfg = load_config()
    loader = SkillsLoader(cfg.workspace_path)
    skills = loader.list_skills(filter_unavailable=False)
    if not skills:
        console.print("No skills found.")
        return

    table = Table(title="Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="green")
    table.add_column("Path", style="dim")
    for s in skills:
        table.add_row(s.get("name", ""), s.get("source", ""), s.get("path", ""))
    console.print(table)


@skills_app.command("add")
def skills_add(
    source: str = typer.Argument(..., help="owner/repo or GitHub URL"),
    skill: str | None = typer.Option(None, "--skill", help="Only install a specific skill directory name"),
    replace: bool = typer.Option(False, "--replace", help="Replace existing skill dirs if present"),
):
    """Install skills into the current workspace by cloning a repo and copying SKILL.md directories."""
    from kyber.config.loader import load_config
    from kyber.skillhub.manager import install_from_source

    cfg = load_config()
    res = install_from_source(
        source,
        skill=skill,
        replace=replace,
        skills_dir=cfg.workspace_path / "skills",
    )
    installed = res.get("installed") or []
    if installed:
        console.print(f"[green]✓[/green] Installed: {', '.join(installed)}")
    else:
        console.print("[yellow]Nothing installed[/yellow] (already present?)")


@skills_app.command("remove")
def skills_remove(name: str = typer.Argument(..., help="Skill directory name under workspace/skills")):
    """Remove a skill from the current workspace."""
    from kyber.config.loader import load_config
    from kyber.skillhub.manager import remove_skill

    cfg = load_config()
    remove_skill(name, skills_dir=cfg.workspace_path / "skills")
    console.print(f"[green]✓[/green] Removed: {name}")


@skills_app.command("update-all")
def skills_update_all():
    """Update all workspace skill sources recorded in the manifest."""
    from kyber.config.loader import load_config
    from kyber.skillhub.manager import update_all

    cfg = load_config()
    res = update_all(replace=True, skills_dir=cfg.workspace_path / "skills")
    updated = res.get("updated") or []
    ok = [u for u in updated if not u.get("error")]
    bad = [u for u in updated if u.get("error")]
    console.print(f"[green]✓[/green] Updated sources: {len(ok)}")
    if bad:
        console.print(f"[red]✗[/red] Failed sources: {len(bad)}")
        for u in bad:
            console.print(f"  - {u.get('source')}: {u.get('error')}")


@skills_app.command("search")
def skills_search(query: str = typer.Argument(..., help="Search skills.sh")):
    """Search skills.sh and print top results."""
    import asyncio

    from kyber.skillhub.skills_sh import search_skills_sh

    results = asyncio.run(search_skills_sh(query, limit=10))
    if not results:
        console.print("No results.")
        return
    table = Table(title=f"skills.sh results for: {query}")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="green")
    table.add_column("Installs", style="yellow", justify="right")
    table.add_column("ID", style="dim")
    for r in results:
        table.add_row(r.get("name", ""), r.get("source", ""), str(r.get("installs", 0)), r.get("id", ""))
    console.print(table)


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from kyber.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # WhatsApp
    wa = config.channels.whatsapp
    table.add_row(
        "WhatsApp",
        "✓" if wa.enabled else "✗",
        wa.bridge_url
    )

    # Telegram
    tg = config.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row(
        "Telegram",
        "✓" if tg.enabled else "✗",
        tg_config
    )

    # Discord
    dc = config.channels.discord
    dc_config = f"token: {dc.token[:10]}..." if dc.token else "[dim]not configured[/dim]"
    table.add_row(
        "Discord",
        "✓" if dc.enabled else "✗",
        dc_config
    )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess
    
    # User's bridge location
    user_bridge = Path.home() / ".kyber" / "bridge"
    
    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge
    
    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)
    
    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # kyber/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)
    
    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge
    
    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: uv tool install --force kyber-chat")
        raise typer.Exit(1)
    
    console.print(f"{__logo__} Setting up bridge...")
    
    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))
    
    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)
        
        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)
        
        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)
    
    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import subprocess
    
    bridge_dir = _get_bridge_dir()
    
    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")
    
    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List scheduled jobs."""
    from kyber.cron.paths import get_cron_store_path
    from kyber.cron.service import CronService
    
    store_path = get_cron_store_path()
    service = CronService(store_path)
    
    jobs = service.list_jobs(include_disabled=all)
    
    if not jobs:
        console.print("No scheduled jobs.")
        return
    
    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")
    
    import time
    for job in jobs:
        # Format schedule
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = job.schedule.expr or ""
        else:
            sched = "one-time"
        
        # Format next run
        next_run = ""
        if job.state.next_run_at_ms:
            next_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.state.next_run_at_ms / 1000))
            next_run = next_time
        
        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"
        
        table.add_row(job.id, job.name, sched, status, next_run)
    
    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="Deliver response to channel"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(None, "--channel", help="Channel for delivery (e.g. 'telegram', 'whatsapp')"),
):
    """Add a scheduled job."""
    from kyber.cron.paths import get_cron_store_path
    from kyber.cron.service import CronService
    from kyber.cron.types import CronSchedule
    
    # Determine schedule type
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
    elif at:
        import datetime
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)
    
    store_path = get_cron_store_path()
    service = CronService(store_path)
    
    job = service.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        to=to,
        channel=channel,
    )
    
    console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
):
    """Remove a scheduled job."""
    from kyber.cron.paths import get_cron_store_path
    from kyber.cron.service import CronService
    
    store_path = get_cron_store_path()
    service = CronService(store_path)
    
    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
):
    """Enable or disable a job."""
    from kyber.cron.paths import get_cron_store_path
    from kyber.cron.service import CronService
    
    store_path = get_cron_store_path()
    service = CronService(store_path)
    
    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    force: bool = typer.Option(False, "--force", "-f", help="Run even if disabled"),
):
    """Manually run a job."""
    from kyber.cron.paths import get_cron_store_path
    from kyber.cron.service import CronService
    
    store_path = get_cron_store_path()
    service = CronService(store_path)
    
    async def run():
        return await service.run_job(job_id, force=force)
    
    if asyncio.run(run()):
        console.print(f"[green]✓[/green] Job executed")
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show kyber status."""
    from kyber.config.loader import load_config, get_config_path, get_env_path, _config_has_secrets

    config_path = get_config_path()
    env_path = get_env_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} kyber Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Secrets: {env_path} {'[green]✓[/green]' if env_path.exists() else '[yellow]missing[/yellow]'}")
    if _config_has_secrets():
        console.print(f"  [yellow]⚠  config.json has plaintext keys — run [cyan]kyber migrate-secrets[/cyan][/yellow]")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        console.print(f"Provider: {config.get_provider_name() or 'not set'}")
        console.print(f"Model: {config.get_model()}")
        
        # Check API keys
        has_openrouter = bool(config.providers.openrouter.api_key)
        has_anthropic = bool(config.providers.anthropic.api_key)
        has_openai = bool(config.providers.openai.api_key)
        has_gemini = bool(config.providers.gemini.api_key)
        
        console.print(f"OpenRouter API: {'[green]✓[/green]' if has_openrouter else '[dim]not set[/dim]'}")
        console.print(f"Anthropic API: {'[green]✓[/green]' if has_anthropic else '[dim]not set[/dim]'}")
        console.print(f"OpenAI API: {'[green]✓[/green]' if has_openai else '[dim]not set[/dim]'}")
        console.print(f"Gemini API: {'[green]✓[/green]' if has_gemini else '[dim]not set[/dim]'}")
        
        customs = config.providers.custom
        if customs:
            for cp in customs:
                name = cp.name or "unnamed"
                has = bool(cp.api_key)
                console.print(f"{name} (custom): {'[green]✓[/green]' if has else '[dim]not set[/dim]'}")


# ============================================================================
# Restart Commands
# ============================================================================

restart_app = typer.Typer(help="Restart kyber services")
app.add_typer(restart_app, name="restart")


@restart_app.command("gateway")
def restart_gateway():
    """Restart the kyber gateway service."""
    from kyber.dashboard.server import _restart_gateway_service

    console.print(f"{__logo__} Restarting gateway...")
    ok, msg = _restart_gateway_service()
    if ok:
        console.print(f"[green]✓[/green] {msg}")
    else:
        console.print(f"[red]✗[/red] {msg}")
        raise typer.Exit(1)


@restart_app.command("dashboard")
def restart_dashboard():
    """Restart the kyber dashboard service."""
    from kyber.dashboard.server import _restart_dashboard_service

    console.print(f"{__logo__} Restarting dashboard...")
    ok, msg = _restart_dashboard_service()
    if ok:
        console.print(f"[green]✓[/green] {msg}")
    else:
        console.print(f"[red]✗[/red] {msg}")
        raise typer.Exit(1)


# ============================================================================
# System Service Management
# ============================================================================

service_app = typer.Typer(help="Manage the background gateway + dashboard services")
app.add_typer(service_app, name="service")


def _render_service_info(info, action: str) -> None:
    """Pretty-print a ServiceInfo block."""
    from kyber.service import ServiceInfo

    assert isinstance(info, ServiceInfo)
    console.print(f"[dim]Backend:[/dim] {info.backend}")
    for unit in (info.gateway, info.dashboard):
        state = "[green]active[/green]" if unit.active else "[yellow]inactive[/yellow]"
        file_bit = "✓" if unit.installed else "✗"
        console.print(
            f"  {unit.name:<9}  file: {file_bit}  status: {state}  "
            f"[dim]({unit.identifier})[/dim]"
        )
    if action == "install" and info.gateway.active and info.dashboard.active:
        console.print("[green]Services installed and running.[/green]")
    elif action == "install":
        console.print(
            "[yellow]Services installed but one or more did not start.[/yellow] "
            "Run `kyber service status` to check."
        )


@service_app.command("install")
def service_install():
    """Install and start the gateway + dashboard as background services.

    Uses launchd on macOS and systemd --user on Linux. Safe to run
    repeatedly — rewrites the unit files and reloads.
    """
    from kyber.service import install_services, UnsupportedPlatformError

    console.print(f"{__logo__} Installing kyber services...")
    try:
        info = install_services()
    except UnsupportedPlatformError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    _render_service_info(info, "install")


@service_app.command("uninstall")
def service_uninstall():
    """Stop and remove the background gateway + dashboard services."""
    from kyber.service import uninstall_services, UnsupportedPlatformError

    console.print(f"{__logo__} Uninstalling kyber services...")
    try:
        info = uninstall_services()
    except UnsupportedPlatformError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    _render_service_info(info, "uninstall")
    console.print(
        "[dim]You can still run `kyber gateway` and `kyber dashboard` manually.[/dim]"
    )


@service_app.command("status")
def service_status_cmd():
    """Show whether gateway + dashboard services are installed and running."""
    from kyber.service import service_status, UnsupportedPlatformError

    try:
        info = service_status()
    except UnsupportedPlatformError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    _render_service_info(info, "status")


@service_app.command("restart")
def service_restart():
    """Reload the unit files and restart both services."""
    from kyber.service import restart_services, UnsupportedPlatformError

    console.print(f"{__logo__} Restarting kyber services...")
    try:
        info = restart_services()
    except UnsupportedPlatformError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    _render_service_info(info, "restart")


@app.command("upgrade")
def upgrade_cmd():
    """One-shot: install latest kyber-chat from PyPI + fully restart services.

    Does the whole dance so you don't have to:

    1. Records the currently installed version.
    2. Runs ``uv tool install --force --no-cache kyber-chat`` to pull the
       newest wheel from PyPI (bypassing any stale uv cache).
    3. Kills any stray ``kyber gateway`` / ``kyber dashboard`` processes
       (including ones systemd/launchd has lost track of).
    4. Restarts the service-managed copies so they load the new code.
    5. Polls the gateway's ``/version`` endpoint and confirms it reports
       the same version the installer just put on disk.

    If step 5 fails it prints what it saw so you can tell me.
    """
    import shutil
    import subprocess
    import time as _time
    import urllib.request

    from kyber import __version__ as pre_version
    from kyber.config.loader import load_config
    from kyber.service import (
        kill_orphan_kyber_processes,
        restart_services,
        UnsupportedPlatformError,
    )

    console.print(f"{__logo__} Kyber upgrade")
    console.print(f"  [dim]current installed:[/dim] {pre_version}")

    uv_path = shutil.which("uv")
    if uv_path is None:
        console.print(
            "[red]`uv` is not on PATH.[/red] Install uv or upgrade manually: "
            "[cyan]pip install --upgrade kyber-chat[/cyan]"
        )
        raise typer.Exit(1)

    console.print("  [dim]installing latest wheel from PyPI…[/dim]")
    try:
        result = subprocess.run(
            [uv_path, "tool", "install", "--force", "--no-cache", "kyber-chat"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        console.print("[red]`uv tool install` timed out after 180s.[/red]")
        raise typer.Exit(1)

    if result.returncode != 0:
        console.print("[red]uv tool install failed:[/red]")
        if result.stderr:
            console.print(result.stderr.strip()[:2000])
        raise typer.Exit(result.returncode)

    console.print("  [green]✓[/green] wheel installed")

    console.print("  [dim]stopping running gateway + dashboard…[/dim]")
    killed = kill_orphan_kyber_processes()
    if killed:
        # This includes the healthy service-managed processes (1 gateway +
        # 1 dashboard) that we're about to restart. Counts above 2 usually
        # mean you had a manually-run copy alongside the service; those
        # get cleaned up here too.
        console.print(
            f"  [dim]stopped {len(killed)} process(es) "
            "(the running services + any manually-started copies)[/dim]"
        )

    console.print("  [dim]restarting services…[/dim]")
    try:
        info = restart_services()
    except UnsupportedPlatformError as e:
        console.print(
            f"[yellow]{e}[/yellow] — install complete, restart your "
            "`kyber gateway` / `kyber dashboard` manually."
        )
        raise typer.Exit(0)

    # Wait for the gateway to report the new version on its /version endpoint.
    cfg = load_config()
    port = cfg.gateway.port
    url = f"http://127.0.0.1:{port}/version"
    expected = _read_expected_version()

    # Gateway startup does a lot (agent init, tool discovery, channels,
    # cron, heartbeat). On a busy VPS this can take 30-60s; poll patiently
    # and print progress every 5s so the user knows we're not hung.
    running: str | None = None
    total_wait = 90.0
    deadline = _time.monotonic() + total_wait
    last_dot = _time.monotonic()
    import json as _json

    console.print(f"  [dim]waiting for gateway to report version on {url}…[/dim]")
    while _time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310
                if resp.status == 200:
                    running = str(_json.loads(resp.read().decode("utf-8")).get("version") or "")
                    if running == expected:
                        break
        except Exception:
            pass
        if _time.monotonic() - last_dot >= 5.0:
            elapsed = int(_time.monotonic() - (deadline - total_wait))
            console.print(f"  [dim]… still waiting ({elapsed}s)[/dim]")
            last_dot = _time.monotonic()
        _time.sleep(0.5)

    if running is None:
        console.print(
            f"[yellow]Upgrade finished but the gateway didn't respond to /version "
            f"within {int(total_wait)}s. Services report active, so the process "
            "is up — it may be slow to bootstrap (channels/skills/MCP). Tail:\n"
            "  [cyan]journalctl --user -u kyber-gateway.service -n 40 --no-pager[/cyan] "
            "(Linux)\n"
            "  [cyan]tail -n 60 ~/.kyber/logs/gateway.err.log[/cyan] (macOS)[/yellow]"
        )
    elif running != expected:
        console.print(
            f"[yellow]Gateway is still reporting version {running} (expected "
            f"{expected}). Something held the old process alive — try:[/yellow]"
        )
        console.print(
            "  [cyan]pkill -9 -f 'kyber gateway'; pkill -9 -f 'kyber dashboard'; "
            "kyber service restart[/cyan]"
        )
    else:
        console.print(f"  [green]✓[/green] gateway reports {running} — upgrade complete")

    _render_service_info(info, "restart")


def _read_expected_version() -> str:
    """Read kyber's __version__ from the freshly-installed wheel on disk.

    Importing ``kyber.__version__`` inside this process gets us the version
    we booted with (the OLD one). We want what the just-installed wheel
    advertises, so we shell out to the tool's own Python binary.
    """
    import shutil
    import subprocess
    import sys

    kyber_tool_path = shutil.which("kyber")
    if kyber_tool_path is None:
        return ""
    try:
        result = subprocess.run(
            [kyber_tool_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    out = (result.stdout or "").strip()
    if " " in out:
        out = out.split()[-1]
    # `kyber --version` prefixes with "v"; the gateway's /version endpoint
    # returns the bare semver. Strip so the comparison can succeed.
    return out.lstrip("v").strip()


# ============================================================================
# Kyber Network
# ============================================================================

network_app = typer.Typer(
    help="Pair Kyber instances across machines and share state between them"
)
app.add_typer(network_app, name="network")


def _save_network_role(role: str) -> None:
    """Persist a new role into config.json without touching other fields."""
    from kyber.config.loader import load_config, save_config

    cfg = load_config()
    cfg.network.role = role
    save_config(cfg)


@network_app.command("status")
def network_status():
    """Show this machine's network mode and paired peers."""
    from kyber.config.loader import load_config
    from kyber.network.host import host_list_peers_with_status
    from kyber.network.state import ROLE_HOST, ROLE_SPOKE, load_state
    from rich.panel import Panel
    from rich.table import Table

    cfg = load_config()
    state = load_state()
    role = (cfg.network.role or "standalone").strip().lower()

    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column()
    table.add_row("role", role)
    table.add_row("peer id", state.peer_id or "[dim](none)[/dim]")
    table.add_row("name", state.name or "[dim](none)[/dim]")

    if role == ROLE_HOST:
        peers = host_list_peers_with_status()
        if peers:
            peer_lines = "\n".join(
                f"[cyan]{p['name']}[/cyan] "
                f"{'[green]●[/green]' if p['connected'] else '[yellow]●[/yellow]'} "
                f"[dim]{p['peer_id'][:12]}…[/dim]"
                for p in peers
            )
            table.add_row("peers", peer_lines)
        else:
            table.add_row("peers", "[dim](none yet — run `kyber network pair`)[/dim]")
    elif role == ROLE_SPOKE:
        if state.host_peer is not None:
            table.add_row("host", f"[cyan]{state.host_peer.name}[/cyan] [dim]{state.host_url}[/dim]")
            from kyber.network.spoke import get_spoke_client

            st = get_spoke_client().status
            running = "[green]running[/green]" if st["running"] else "[yellow]not running[/yellow]"
            table.add_row("link", f"{running} [dim]{st.get('last_error') or ''}[/dim]")
        else:
            table.add_row("host", "[yellow]not paired — run `kyber network join`[/yellow]")

    console.print(Panel(table, title="Kyber Network", border_style="magenta", expand=False))


@network_app.command("pair")
def network_pair(
    name: str | None = typer.Option(
        None, "--name", help="Optional label for the spoke you're about to pair."
    ),
):
    """Generate a one-time pairing code (run on the host).

    The host must already be configured with ``network.role = host`` — this
    command flips the role automatically if it's still the default.

    The code expires in 5 minutes and can be used exactly once. Run
    ``kyber network join <host-url> <code>`` on the other machine.
    """
    from kyber.config.loader import load_config
    from kyber.network.host import host_generate_pairing_code
    from kyber.network.state import ROLE_HOST, get_or_create_identity

    from kyber.network.state import load_state, save_state

    cfg = load_config()
    role = (cfg.network.role or "standalone").strip().lower()
    if role != ROLE_HOST:
        console.print(
            "[yellow]This instance isn't in host mode yet — switching to host.[/yellow]"
        )
        _save_network_role(ROLE_HOST)
    get_or_create_identity()  # ensure peer_id + name exist

    # Also flip the state-file role so the pair + pair-code endpoints (which
    # read state, not config) accept requests without a gateway restart.
    state = load_state()
    if state.role != ROLE_HOST:
        state.role = ROLE_HOST
        save_state(state)

    code = host_generate_pairing_code(expected_name=name)
    gw_host = cfg.gateway.host
    gw_port = cfg.gateway.port
    display_host = (
        "127.0.0.1" if gw_host in ("", "0.0.0.0", "::", "::0") else gw_host
    )
    url = f"http://{display_host}:{gw_port}"
    console.print(
        f"[bold magenta]Pairing code:[/bold magenta] [cyan]{code}[/cyan]  "
        f"[dim](expires in 5 minutes)[/dim]\n"
        f"Run on the other machine:\n"
        f"  [cyan]kyber network join {url} {code} --as <name>[/cyan]\n"
        f"[yellow]Note:[/yellow] the URL must be reachable from that machine — "
        f"use a LAN IP or Tailscale address if they're on different networks."
    )
    if role == ROLE_HOST:
        from kyber.network.state import load_state

        live = load_state()
        if not live.paired_peers:
            console.print(
                "[dim]Restart the gateway after pairing so the host handler is "
                "mounted on the listening port: `kyber service restart`.[/dim]"
            )


@network_app.command("join")
def network_join(
    host_url: str = typer.Argument(..., help="Host URL, e.g. http://10.0.0.5:18790"),
    code: str = typer.Argument(..., help="Pairing code from `kyber network pair`."),
    as_name: str = typer.Option(
        None, "--as", help="Display name for this machine (defaults to hostname)."
    ),
):
    """Pair this machine as a spoke to the given host."""
    import asyncio

    from kyber.network.spoke import pair_with_host
    from kyber.network.state import get_or_create_identity

    get_or_create_identity()
    try:
        state = asyncio.run(
            pair_with_host(host_url, code, display_name=as_name)
        )
    except RuntimeError as e:
        console.print(f"[red]Pairing failed:[/red] {e}")
        raise typer.Exit(1)

    # Flip role to spoke in config.json too.
    _save_network_role("spoke")
    host = state.host_peer
    console.print(
        f"[green]✓[/green] Paired with [cyan]{host.name if host else 'host'}[/cyan] "
        f"at [cyan]{state.host_url}[/cyan] "
        f"as [cyan]{state.name}[/cyan]."
    )
    console.print(
        "[dim]Restart the gateway to start the live link: `kyber service restart`.[/dim]"
    )


@network_app.command("leave")
def network_leave(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
):
    """Disconnect this machine from its host (spoke-side)."""
    from kyber.network.state import (
        ROLE_SPOKE,
        ROLE_STANDALONE,
        load_state,
        save_state,
    )

    state = load_state()
    if not state.host_peer or not state.host_url:
        console.print("[yellow]Not currently paired with a host.[/yellow]")
        raise typer.Exit(0)
    if not yes:
        console.print(
            f"About to forget host [cyan]{state.host_peer.name}[/cyan] at "
            f"[cyan]{state.host_url}[/cyan]."
        )
        if not typer.confirm("Proceed?"):
            raise typer.Exit(0)

    state.role = ROLE_STANDALONE
    state.host_url = ""
    state.host_peer = None
    save_state(state)
    _save_network_role(ROLE_STANDALONE)
    console.print("[green]✓[/green] Removed host pairing. Restart the gateway to take effect.")


@network_app.command("expose")
def network_expose(
    tools: list[str] = typer.Argument(
        ...,
        help='Tool names, OR "all" to expose every registered tool, OR "none" to disable.',
    ),
):
    """Set which local tools paired Kybers can invoke over the network.

    Examples::

        kyber network expose all                   # full mesh (new default)
        kyber network expose none                  # disable remote invocation
        kyber network expose exec read_file        # explicit list

    Writes ``config.network.exposedTools`` and persists. Takes effect on
    the next gateway restart.
    """
    from kyber.config.loader import load_config, save_config

    cfg = load_config()
    normalized = [t.strip() for t in tools if t and t.strip()]
    if len(normalized) == 1 and normalized[0].lower() == "all":
        cfg.network.exposed_tools = ["*"]
    elif len(normalized) == 1 and normalized[0].lower() == "none":
        cfg.network.exposed_tools = []
    else:
        cfg.network.exposed_tools = normalized
    save_config(cfg)
    value = cfg.network.exposed_tools
    if not value:
        human = "[yellow](none — remote invocation disabled)[/yellow]"
    elif value == ["*"]:
        human = "[green]every registered tool[/green]"
    else:
        human = ", ".join(value)
    console.print(
        f"[green]✓[/green] exposedTools set to: {human}\n"
        "[dim]Restart the gateway so it picks up the change: "
        "[cyan]kyber service restart[/cyan][/dim]"
    )


@network_app.command("unpair")
def network_unpair(
    peer_id: str = typer.Argument(..., help="Peer id (or prefix) to remove."),
):
    """Revoke a paired spoke (host-side)."""
    from kyber.network.host import host_unpair
    from kyber.network.state import load_state

    state = load_state()
    matches = [p for p in state.paired_peers if p.peer_id.startswith(peer_id)]
    if not matches:
        console.print(f"[yellow]No paired peer matches id prefix {peer_id!r}.[/yellow]")
        raise typer.Exit(1)
    if len(matches) > 1:
        console.print("[yellow]Ambiguous peer id prefix — be more specific:[/yellow]")
        for p in matches:
            console.print(f"  {p.peer_id}  {p.name}")
        raise typer.Exit(1)
    target = matches[0]
    if host_unpair(target.peer_id):
        console.print(f"[green]✓[/green] Removed peer [cyan]{target.name}[/cyan].")
    else:
        console.print(f"[yellow]Nothing removed for {target.peer_id}.[/yellow]")


# ============================================================================
# Skill Scanner Setup
# ============================================================================


@app.command("setup-skillscanner")
def setup_skillscanner():
    """Install the Cisco AI Defense skill-scanner for skill security scanning."""
    import shutil
    import subprocess as sp

    if shutil.which("skill-scanner"):
        console.print(f"[green]✓[/green] skill-scanner is already installed: {shutil.which('skill-scanner')}")
        result = sp.run(["skill-scanner", "--version"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            console.print(f"  [dim]Version: {result.stdout.strip()}[/dim]")
        return

    if not shutil.which("uv"):
        console.print("[red]✗[/red] uv is not installed. Install it from https://docs.astral.sh/uv/getting-started/installation/")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up Cisco AI Defense skill-scanner...\n")

    console.print("[cyan]Installing cisco-ai-skill-scanner via uv...[/cyan]")
    result = sp.run(
        ["uv", "tool", "install", "cisco-ai-skill-scanner"],
        capture_output=True, text=True, timeout=120,
    )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        console.print(f"[red]✗[/red] Installation failed:\n{stderr[:500]}")
        raise typer.Exit(1)

    if shutil.which("skill-scanner"):
        console.print(f"[green]✓[/green] skill-scanner installed: {shutil.which('skill-scanner')}")
    else:
        console.print("[green]✓[/green] Package installed (skill-scanner may require a shell restart to appear in PATH)")

    console.print(f"\n{__logo__} Skill scanner is ready! The security scan will now include skill security checks.")


# ============================================================================
# OpenCode Setup
# ============================================================================


# ============================================================================
# ClamAV Setup
# ============================================================================


@app.command("setup-clamav")
def setup_clamav():
    """Install and configure ClamAV as a proper system daemon.

    Sets up both clamd (scan daemon) and freshclam (signature updater) as
    system services. Idempotent — re-running fixes broken installs.
    """
    import shutil
    import subprocess as sp

    system = platform.system()

    console.print(f"{__logo__} Setting up ClamAV malware scanner...\n")

    # ── Step 1: Install ClamAV if not present ──
    if not shutil.which("clamscan"):
        if system == "Darwin":
            if not shutil.which("brew"):
                console.print("[red]✗[/red] Homebrew not found. Install from https://brew.sh")
                raise typer.Exit(1)
            console.print("[cyan]Installing ClamAV via Homebrew...[/cyan]")
            result = sp.run(["brew", "install", "clamav"], capture_output=True, text=True)
            if result.returncode != 0:
                console.print(f"[red]✗[/red] brew install failed:\n{result.stderr}")
                raise typer.Exit(1)
            console.print("[green]✓[/green] ClamAV installed")
        elif system == "Linux":
            pkg_cmd = None
            if shutil.which("apt"):
                pkg_cmd = ["sudo", "apt", "install", "-y", "clamav", "clamav-daemon"]
            elif shutil.which("dnf"):
                pkg_cmd = ["sudo", "dnf", "install", "-y", "clamav", "clamav-update", "clamd"]
            elif shutil.which("zypper"):
                pkg_cmd = ["sudo", "zypper", "install", "-y", "clamav"]
            elif shutil.which("pacman"):
                pkg_cmd = ["sudo", "pacman", "-S", "--noconfirm", "clamav"]
            else:
                console.print("[red]✗[/red] No supported package manager (apt, dnf, zypper, pacman).")
                raise typer.Exit(1)
            console.print(f"[cyan]Installing ClamAV...[/cyan]")
            result = sp.run(pkg_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                console.print(f"[red]✗[/red] Install failed:\n{result.stderr}")
                raise typer.Exit(1)
            console.print("[green]✓[/green] ClamAV installed")
        else:
            console.print(f"[red]✗[/red] Unsupported platform: {system}")
            raise typer.Exit(1)
    else:
        console.print(f"[green]✓[/green] ClamAV already installed: {shutil.which('clamscan')}")

    # ── Step 1b: Ensure daemon package is installed on Linux ──
    if system == "Linux" and not shutil.which("clamdscan") and not shutil.which("clamd"):
        daemon_cmd = None
        if shutil.which("apt"):
            daemon_cmd = ["sudo", "apt", "install", "-y", "clamav-daemon"]
        elif shutil.which("dnf"):
            daemon_cmd = ["sudo", "dnf", "install", "-y", "clamd"]
        if daemon_cmd:
            console.print("[cyan]Installing ClamAV daemon package...[/cyan]")
            result = sp.run(daemon_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                console.print("[green]✓[/green] ClamAV daemon installed")
            else:
                console.print("[yellow]⚠[/yellow] Could not install daemon package — scans will use clamscan (slower)")

    # ── Step 2: Configure freshclam + clamd ──
    _ensure_freshclam_config(system)
    _ensure_clamd_config(system)

    # ── Step 3: Stop standalone freshclam (conflicts with service) ──
    if system == "Linux":
        sp.run(["sudo", "systemctl", "stop", "clamav-freshclam"], capture_output=True, text=True)

    # ── Step 4: Initial signature download if needed ──
    db_dir = _find_clamav_db_dir()
    needs_sigs = True
    if db_dir and db_dir.is_dir():
        db_files = [f for f in db_dir.iterdir() if f.suffix in (".cvd", ".cld")]
        if db_files:
            needs_sigs = False
            console.print(f"[green]✓[/green] Signatures: {', '.join(f.name for f in sorted(db_files))}")
    if needs_sigs:
        console.print("[cyan]Downloading virus signatures (this may take a minute)...[/cyan]")
        _run_freshclam_once(system)

    # ── Step 5: Enable and start services ──
    if system == "Linux":
        _setup_linux_clamav_services()
    elif system == "Darwin":
        _setup_macos_clamav_services()

    # ── Step 6: Verify ──
    console.print("")
    ok = _verify_clamav_setup(system)
    if ok:
        console.print(f"\n{__logo__} ClamAV is ready! Scans will use the clamd daemon for fast malware detection.")
    else:
        console.print(f"\n{__logo__} ClamAV is installed but clamd isn't running — scans will fall back to clamscan (slower).")
        console.print("[dim]You can retry [cyan]kyber setup-clamav[/cyan] or start clamd manually to get faster scans.[/dim]")


def _ensure_freshclam_config(system: str):
    """Ensure freshclam.conf is properly configured."""
    import subprocess as sp

    console.print("[cyan]Configuring freshclam...[/cyan]")

    if system == "Darwin":
        prefix_result = sp.run(["brew", "--prefix"], capture_output=True, text=True)
        brew_prefix = prefix_result.stdout.strip() or "/usr/local"
        conf = Path(brew_prefix) / "etc" / "clamav" / "freshclam.conf"
        sample = Path(brew_prefix) / "etc" / "clamav" / "freshclam.conf.sample"
        db_dir = Path(brew_prefix) / "share" / "clamav"

        source = conf if conf.exists() else sample
        if not source.exists():
            console.print(f"  [dim]No freshclam config found[/dim]")
            return

        db_dir.mkdir(parents=True, exist_ok=True)
        lines = source.read_text().splitlines()
        db_dir_set = False
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped == "Example":
                new_lines.append("# Example")
                continue
            if stripped.startswith("DatabaseDirectory") and not stripped.startswith("#"):
                new_lines.append(f"DatabaseDirectory {db_dir}")
                db_dir_set = True
                continue
            if stripped.startswith("#") and "DatabaseDirectory" in stripped and not db_dir_set:
                new_lines.append(f"DatabaseDirectory {db_dir}")
                db_dir_set = True
                continue
            new_lines.append(line)
        if not db_dir_set:
            new_lines.append(f"DatabaseDirectory {db_dir}")
        conf.write_text("\n".join(new_lines) + "\n")
        console.print(f"  [green]✓[/green] {conf}")

    elif system == "Linux":
        for conf_path in [Path("/etc/clamav/freshclam.conf"), Path("/etc/freshclam.conf")]:
            if conf_path.exists():
                lines = conf_path.read_text().splitlines()
                if any(line.strip() == "Example" for line in lines):
                    fixed = [("# Example" if line.strip() == "Example" else line) for line in lines]
                    sp.run(
                        ["sudo", "tee", str(conf_path)],
                        input="\n".join(fixed) + "\n",
                        capture_output=True, text=True,
                    )
                console.print(f"  [green]✓[/green] {conf_path}")
                break


def _ensure_clamd_config(system: str):
    """Ensure clamd.conf is configured with a local socket."""
    import subprocess as sp

    console.print("[cyan]Configuring clamd...[/cyan]")

    if system == "Darwin":
        prefix_result = sp.run(["brew", "--prefix"], capture_output=True, text=True)
        brew_prefix = prefix_result.stdout.strip() or "/usr/local"
        conf = Path(brew_prefix) / "etc" / "clamav" / "clamd.conf"
        sample = Path(brew_prefix) / "etc" / "clamav" / "clamd.conf.sample"
        db_dir = Path(brew_prefix) / "share" / "clamav"
        socket_path = Path(brew_prefix) / "var" / "run" / "clamav" / "clamd.sock"

        source = conf if conf.exists() else sample
        if not source.exists():
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            conf.write_text(
                f"# Kyber-managed clamd config\n"
                f"LocalSocket {socket_path}\n"
                f"DatabaseDirectory {db_dir}\n"
            )
            console.print(f"  [green]✓[/green] Created {conf}")
            return

        lines = source.read_text().splitlines()
        has_socket = False
        db_dir_set = False
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped == "Example":
                new_lines.append("# Example")
                continue
            if stripped.startswith("LocalSocket") and not stripped.startswith("#"):
                has_socket = True
            if stripped.startswith("DatabaseDirectory") and not stripped.startswith("#"):
                new_lines.append(f"DatabaseDirectory {db_dir}")
                db_dir_set = True
                continue
            new_lines.append(line)
        if not has_socket:
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            new_lines.append(f"LocalSocket {socket_path}")
        if not db_dir_set:
            new_lines.append(f"DatabaseDirectory {db_dir}")
        conf.write_text("\n".join(new_lines) + "\n")
        console.print(f"  [green]✓[/green] {conf}")

    elif system == "Linux":
        for conf_path in [Path("/etc/clamav/clamd.conf"), Path("/etc/clamd.conf"), Path("/etc/clamd.d/scan.conf")]:
            if conf_path.exists():
                lines = conf_path.read_text().splitlines()
                needs_write = False
                new_lines_l: list[str] = []
                has_socket = False
                socket_path = "/var/run/clamav/clamd.ctl"
                for line in lines:
                    stripped = line.strip()
                    if stripped == "Example":
                        new_lines_l.append("# Example")
                        needs_write = True
                        continue
                    if "LocalSocket" in stripped and not stripped.startswith("#"):
                        has_socket = True
                        # Extract the configured socket path
                        parts = stripped.split(None, 1)
                        if len(parts) == 2:
                            socket_path = parts[1]
                    new_lines_l.append(line)
                if not has_socket:
                    new_lines_l.append(f"LocalSocket {socket_path}")
                    needs_write = True
                if needs_write:
                    sp.run(
                        ["sudo", "tee", str(conf_path)],
                        input="\n".join(new_lines_l) + "\n",
                        capture_output=True, text=True,
                    )
                # Ensure the socket directory exists with correct ownership
                _ensure_socket_dir(socket_path)
                console.print(f"  [green]✓[/green] {conf_path}")
                return

        # No config found — create one
        debian_conf = Path("/etc/clamav/clamd.conf")
        socket_path = "/var/run/clamav/clamd.ctl"
        sp.run(
            ["sudo", "tee", str(debian_conf)],
            input=(
                "# Kyber-managed clamd config\n"
                f"LocalSocket {socket_path}\n"
                "DatabaseDirectory /var/lib/clamav\n"
                "User clamav\n"
            ),
            capture_output=True, text=True,
        )
        _ensure_socket_dir(socket_path)
        console.print(f"  [green]✓[/green] Created {debian_conf}")


def _run_freshclam_once(system: str):
    """Run freshclam once to download initial signatures."""
    import shutil
    import subprocess as sp

    freshclam = shutil.which("freshclam")
    if not freshclam:
        console.print("[yellow]⚠  freshclam not found[/yellow]")
        return

    if system == "Linux":
        result = sp.run(
            ["sudo", freshclam, "--stdout", "--log=/dev/null"],
            capture_output=True, text=True, timeout=300,
        )
    else:
        result = sp.run(
            [freshclam, "--stdout", "--log=/dev/null"],
            capture_output=True, text=True, timeout=300,
        )

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0:
        if "up to date" in output.lower():
            console.print("[green]✓[/green] Virus signatures already up to date")
        else:
            console.print("[green]✓[/green] Virus signatures downloaded")
    else:
        preview = "\n".join(output.splitlines()[:5]) if output else "(no output)"
        console.print(f"[yellow]⚠  freshclam warnings:[/yellow]\n  {preview}")


def _ensure_socket_dir(socket_path: str):
    """Ensure the directory for the clamd socket exists with correct ownership."""
    import subprocess as sp

    sock_dir = str(Path(socket_path).parent)
    sp.run(["sudo", "mkdir", "-p", sock_dir], capture_output=True, text=True)
    sp.run(["sudo", "chown", "clamav:clamav", sock_dir], capture_output=True, text=True)
    sp.run(["sudo", "chmod", "755", sock_dir], capture_output=True, text=True)


def _setup_linux_clamav_services():
    """Enable and start ClamAV systemd services."""
    import subprocess as sp
    import time

    console.print("[cyan]Enabling ClamAV services...[/cyan]")

    for svc_name in ["clamav-freshclam", "freshclam"]:
        result = sp.run(["sudo", "systemctl", "enable", svc_name], capture_output=True, text=True)
        if result.returncode == 0:
            sp.run(["sudo", "systemctl", "start", svc_name], capture_output=True, text=True)
            # Verify it actually started
            check = sp.run(["systemctl", "is-active", svc_name], capture_output=True, text=True)
            if check.stdout.strip() == "active":
                console.print(f"  [green]✓[/green] {svc_name} enabled and started")
            else:
                console.print(f"  [yellow]⚠[/yellow] {svc_name} enabled but failed to start")
            break
    else:
        console.print("  [yellow]⚠[/yellow] Could not enable freshclam service")

    for svc_name in ["clamav-daemon", "clamd@scan", "clamd"]:
        result = sp.run(["sudo", "systemctl", "enable", svc_name], capture_output=True, text=True)
        if result.returncode == 0:
            sp.run(["sudo", "systemctl", "start", svc_name], capture_output=True, text=True)
            # Give clamd a moment to start — it loads signatures on startup
            time.sleep(2)
            check = sp.run(["systemctl", "is-active", svc_name], capture_output=True, text=True)
            if check.stdout.strip() == "active":
                console.print(f"  [green]✓[/green] {svc_name} enabled and started")
            else:
                # Check why it failed
                journal = sp.run(
                    ["journalctl", "-u", svc_name, "-n", "5", "--no-pager", "-q"],
                    capture_output=True, text=True,
                )
                console.print(f"  [yellow]⚠[/yellow] {svc_name} enabled but failed to start")
                if journal.stdout.strip():
                    for jline in journal.stdout.strip().splitlines()[-2:]:
                        console.print(f"    [dim]{jline.strip()}[/dim]")
            break
    else:
        console.print("  [yellow]⚠[/yellow] Could not enable clamd service")


def _setup_macos_clamav_services():
    """Start ClamAV services on macOS via Homebrew."""
    import subprocess as sp

    console.print("[cyan]Starting ClamAV services...[/cyan]")
    result = sp.run(["brew", "services", "start", "clamav"], capture_output=True, text=True)
    if result.returncode == 0:
        console.print("  [green]✓[/green] ClamAV services started via Homebrew")
    else:
        console.print("  [yellow]⚠[/yellow] Could not auto-start. Run: brew services start clamav")


def _verify_clamav_setup(system: str) -> bool:
    """Verify ClamAV daemon is running and responsive. Returns True if clamd is fully working."""
    import shutil
    import subprocess as sp

    console.print("[cyan]Verifying setup...[/cyan]")
    clamd_ok = False

    if shutil.which("clamdscan"):
        result = sp.run(["clamdscan", "--ping", "3"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            console.print("  [green]✓[/green] clamd is running and responsive")
            clamd_ok = True
        else:
            console.print("  [yellow]⚠[/yellow] clamd not responding yet — may still be loading signatures")
            console.print("  [dim]Give it a minute, then try: clamdscan --ping 5[/dim]")
    else:
        console.print("  [yellow]⚠[/yellow] clamdscan not found — will fall back to clamscan (slower)")

    if system == "Linux":
        for svc in ["clamav-freshclam", "freshclam"]:
            result = sp.run(["systemctl", "is-active", svc], capture_output=True, text=True)
            if result.stdout.strip() == "active":
                console.print(f"  [green]✓[/green] {svc}: active (auto-updates signatures)")
                break
        else:
            console.print("  [yellow]⚠[/yellow] freshclam service not active")

        for svc in ["clamav-daemon", "clamd@scan", "clamd"]:
            result = sp.run(["systemctl", "is-active", svc], capture_output=True, text=True)
            if result.stdout.strip() == "active":
                console.print(f"  [green]✓[/green] {svc}: active")
                clamd_ok = True
                break
        else:
            console.print("  [yellow]⚠[/yellow] clamd service not active")

    return clamd_ok


def _find_clamav_db_dir() -> Path | None:
    """Locate the ClamAV signature database directory."""
    import subprocess as sp

    system = platform.system()
    candidates: list[Path] = []

    if system == "Darwin":
        prefix_result = sp.run(["brew", "--prefix"], capture_output=True, text=True)
        brew_prefix = prefix_result.stdout.strip() or "/usr/local"
        candidates.append(Path(brew_prefix) / "share" / "clamav")

    candidates.extend([
        Path("/var/lib/clamav"),
        Path("/usr/local/share/clamav"),
        Path("/opt/homebrew/share/clamav"),
    ])

    for p in candidates:
        if p.is_dir():
            return p
    return None


# ============================================================================
# Background ClamAV Scan
# ============================================================================


@app.command("clamscan")
def clamscan_cmd(
    schedule: bool = typer.Option(False, "--schedule", help="Register a daily cron job instead of running now"),
):
    """Run a ClamAV malware scan in the background.

    By default, runs the scan immediately and saves results to
    ~/.kyber/security/clamscan/. Use --schedule to register a daily
    cron job that runs the scan automatically.
    """
    if schedule:
        _register_clamscan_cron()
        return

    import shutil
    if not shutil.which("clamscan") and not shutil.which("clamdscan"):
        console.print("[red]✗[/red] ClamAV not installed. Run [cyan]kyber setup-clamav[/cyan] first.")
        raise typer.Exit(1)

    console.print(f"{__logo__} Running ClamAV malware scan...\n")
    console.print("[dim]Results are saved to ~/.kyber/security/clamscan/[/dim]\n")

    from kyber.security.clamscan import run_clamscan
    report = run_clamscan()

    status = report.get("status", "error")
    duration = report.get("duration_seconds", 0)
    infected = report.get("infected_files", [])

    if status == "clean":
        console.print(f"[green]✓[/green] No threats detected ({duration}s)")
    elif status == "threats_found":
        console.print(f"[red]✗[/red] {len(infected)} threat(s) detected ({duration}s):")
        for inf in infected:
            console.print(f"  [red]•[/red] {inf.get('file', '?')} — {inf.get('threat', '?')}")
    else:
        console.print(f"[yellow]⚠[/yellow] Scan error: {report.get('error', 'unknown')} ({duration}s)")

    console.print(f"\n[dim]Report saved to ~/.kyber/security/clamscan/latest.json[/dim]")


def _register_clamscan_cron():
    """Register a daily ClamAV scan as a kyber cron job (manual CLI path)."""
    import asyncio

    console.print(f"{__logo__} Setting up daily ClamAV scan...\n")

    try:
        from kyber.config.loader import load_config
        config = load_config()
    except Exception as e:
        console.print(f"[red]✗[/red] Failed to load config: {e}")
        raise typer.Exit(1)

    from kyber.cron.service import CronService
    from kyber.cron.types import CronSchedule

    cron = CronService(config)

    CLAMSCAN_JOB_ID = "kyber-clamscan"

    # Check if already registered
    for job in cron.list_jobs(include_disabled=True):
        if job.id == CLAMSCAN_JOB_ID or "clamscan" in job.name.lower() or "clamav" in job.name.lower():
            console.print(f"[green]✓[/green] Daily ClamAV scan already registered (job: {job.id})")
            if not job.enabled:
                cron.enable_job(job.id, enabled=True)
                console.print("[green]✓[/green] Re-enabled the job")
            return

    # Register new job — daily at 3 AM
    job = cron.add_job(
        name="Daily ClamAV Scan",
        schedule=CronSchedule(kind="cron", expr="0 3 * * *"),
        message="Run a background ClamAV malware scan: from kyber.security.clamscan import run_clamscan; run_clamscan()",
        job_id=CLAMSCAN_JOB_ID,
    )

    console.print(f"[green]✓[/green] Daily ClamAV scan registered (job: {job.id})")
    console.print("[dim]Runs daily at 3:00 AM. View/manage in the dashboard under Cron Jobs.[/dim]")


def _ensure_clamscan_cron(cron_svc) -> None:
    """Auto-register the daily ClamAV cron job if ClamAV is installed.

    Called at startup so the user never has to manually schedule it.
    Also kicks off the first scan in a background thread if no results exist yet.
    """
    import shutil

    if not shutil.which("clamscan") and not shutil.which("clamdscan"):
        return  # ClamAV not installed, nothing to do

    from kyber.cron.types import CronSchedule

    CLAMSCAN_JOB_ID = "kyber-clamscan"

    # Clean up any duplicate clamscan jobs from earlier bugs
    seen_job: object = None
    for job in cron_svc.list_jobs(include_disabled=True):
        is_clamscan = (
            job.id == CLAMSCAN_JOB_ID
            or "clamscan" in job.name.lower()
            or "clamav" in job.name.lower()
        )
        if is_clamscan:
            if seen_job is not None:
                # Duplicate — remove it
                cron_svc.remove_job(job.id)
            else:
                seen_job = job
                if not job.enabled:
                    cron_svc.enable_job(job.id, enabled=True)

    # Normalize the surviving job to the canonical ID so future lookups work
    if seen_job is not None and seen_job.id != CLAMSCAN_JOB_ID:
        cron_svc.remove_job(seen_job.id)
        seen_job = None  # Re-create with the correct ID below

    # Register with fixed ID (add_job deduplicates by ID)
    if seen_job is None:
        cron_svc.add_job(
            name="Daily ClamAV Scan",
            schedule=CronSchedule(kind="cron", expr="0 3 * * *"),
            message="Run background ClamAV scan",
            job_id=CLAMSCAN_JOB_ID,
        )
        console.print("[green]✓[/green] Auto-registered daily ClamAV scan (3:00 AM)")

    # If no scan results exist yet, kick off the first scan in a background thread
    from kyber.security.clamscan import get_latest_report, get_running_state
    if get_latest_report() is None and get_running_state() is None:
        import threading
        from kyber.security.clamscan import run_clamscan

        def _first_scan():
            try:
                run_clamscan()
            except Exception:
                pass  # Non-critical, will run again on schedule

        t = threading.Thread(target=_first_scan, daemon=True, name="clamscan-initial")
        t.start()
        console.print("[cyan]⏳[/cyan] Running initial ClamAV scan in background…")


if __name__ == "__main__":
    app()
