"""CLI commands for kyber."""

import asyncio
import platform
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
    if not skills_readme.exists():
        skills_readme.write_text(
            "# Skills\n\n"
            "Drop custom skills here as folders containing a `SKILL.md`.\n\n"
            "Example:\n"
            "- `skills/my-skill/SKILL.md`\n\n"
            "Kyber also supports managed skills in `~/.kyber/skills/<skill>/SKILL.md`.\n"
        )
        console.print("  [dim]Created skills/README.md[/dim]")


# ============================================================================
# Shared Orchestrator Factory
# ============================================================================


def _create_orchestrator(
    config,
    bus,
    *,
    background_progress_updates: bool = False,
    task_history_path: Path | None = None,
):
    """Create an Orchestrator with providers from config.

    Shared by the ``gateway`` and ``agent`` commands so provider/model
    resolution lives in one place.
    """
    from kyber.providers.litellm_provider import LiteLLMProvider
    from kyber.agent.orchestrator import Orchestrator

    # ── Chat provider ──
    chat_details = config.get_chat_provider_details()
    chat_api_key = chat_details.get("api_key")
    chat_api_base = chat_details.get("api_base")
    chat_provider_name = chat_details.get("provider_name") or config.get_provider_name()
    chat_model = config.get_chat_model()
    chat_is_bedrock = chat_model.startswith("bedrock/")

    if chat_provider_name and not chat_api_key and not chat_is_bedrock:
        console.print(f"[red]Error: No API key configured for chat provider {chat_provider_name}.[/red]")
        console.print(f"Set one in ~/.kyber/.env: KYBER_PROVIDERS__{chat_provider_name.upper()}__API_KEY=your-key")
        raise typer.Exit(1)

    if not chat_api_key and not chat_is_bedrock:
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.kyber/.env: KYBER_PROVIDERS__OPENROUTER__API_KEY=your-key")
        raise typer.Exit(1)

    chat_provider = LiteLLMProvider(
        api_key=chat_api_key,
        api_base=chat_api_base,
        default_model=chat_model,
        provider_name=chat_provider_name,
        is_custom=chat_details.get("is_custom", False),
    )

    # ── Task provider ──
    task_details = config.get_task_provider_details()
    task_provider_name = task_details.get("provider_name") or chat_provider_name
    task_api_key = task_details.get("api_key")
    task_api_base = task_details.get("api_base")
    task_model = config.get_task_model()
    task_is_bedrock = task_model.startswith("bedrock/")

    if (task_provider_name == chat_provider_name
        and task_api_key == chat_api_key
        and task_api_base == chat_api_base):
        task_provider = chat_provider
    else:
        if task_provider_name and not task_api_key and not task_is_bedrock:
            console.print(f"[red]Error: No API key configured for task provider {task_provider_name}.[/red]")
            console.print(f"Set one in ~/.kyber/.env: KYBER_PROVIDERS__{task_provider_name.upper()}__API_KEY=your-key")
            raise typer.Exit(1)
        task_provider = LiteLLMProvider(
            api_key=task_api_key,
            api_base=task_api_base,
            default_model=task_model,
            provider_name=task_provider_name,
            is_custom=task_details.get("is_custom", False),
        )

    # ── Persona ──
    workspace = config.workspace_path
    soul_path = workspace / "SOUL.md"
    persona = ""
    if soul_path.exists():
        persona = soul_path.read_text(encoding="utf-8")

    # ── Orchestrator ──
    return Orchestrator(
        bus=bus,
        provider=chat_provider,
        workspace=workspace,
        persona_prompt=persona,
        model=chat_model,
        brave_api_key=config.tools.web.search.api_key or None,
        background_progress_updates=background_progress_updates,
        task_history_path=task_history_path,
        task_provider=task_provider,
        task_model=task_model,
        timezone=config.agents.defaults.timezone or None,
        exec_timeout=config.tools.exec.timeout,
    )


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port (defaults to config.gateway.port)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the kyber gateway."""
    from kyber.config.loader import load_config, get_data_dir
    from kyber.config.loader import save_config
    from kyber.bus.queue import MessageBus
    from kyber.channels.manager import ChannelManager
    from kyber.cron.service import CronService
    from kyber.cron.types import CronJob
    from kyber.heartbeat.service import HeartbeatService
    from kyber.gateway.api import create_gateway_app
    
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
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "skills").mkdir(exist_ok=True)
    
    # Create components
    bus = MessageBus()
    
    agent = _create_orchestrator(
        config,
        bus,
        background_progress_updates=getattr(config.agents.defaults, "background_progress_updates", True),
        task_history_path=get_data_dir() / "tasks" / "history.jsonl",
    )
    
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

        # When the job has delivery configured, route through that channel
        # so spawned background workers send completions to the right place.
        channel = "cli"
        chat_id = "direct"
        if job.payload.deliver and job.payload.to:
            channel = job.payload.channel or "discord"
            chat_id = job.payload.to

        response = await agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
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
    
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    user_tz = config.agents.defaults.timezone or None
    cron = CronService(cron_store_path, on_job=on_cron_job, timezone=user_tz)

    # Auto-register daily ClamAV scan if ClamAV is installed
    _ensure_clamscan_cron(cron)

    # Create heartbeat service
    async def on_heartbeat(prompt: str) -> str:
        """Execute heartbeat through the agent."""
        return await agent.process_direct(prompt, session_key="heartbeat")
    
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,  # 30 minutes
        enabled=True
    )
    
    # Create channel manager
    channels = ChannelManager(config, bus)
    
    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")
    
    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")
    
    console.print(f"[green]✓[/green] Heartbeat: every 30m")

    async def run():
        try:
            # Start local gateway API for dashboard task management.
            import uvicorn
            api_app = create_gateway_app(agent, config.dashboard.auth_token)
            api_config = uvicorn.Config(
                api_app,
                host=config.gateway.host,
                port=port,
                log_level="warning",
                access_log=False,
            )
            api_server = uvicorn.Server(api_config)
            api_task = asyncio.create_task(api_server.serve())

            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
                api_task,
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()
    
    asyncio.run(run())




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
    """Start the kyber web dashboard."""
    import secrets
    import uvicorn

    from kyber.config.loader import load_config, save_config
    from kyber.dashboard.server import create_dashboard_app
    from kyber.config.loader import get_data_dir
    from kyber.logging.error_store import init_error_store

    config = load_config()
    dash = config.dashboard

    if host:
        dash.host = host
    if port:
        dash.port = port

    if not dash.auth_token.strip():
        dash.auth_token = secrets.token_urlsafe(32)
        save_config(config)
        console.print("[green]✓[/green] Generated new dashboard token and saved to config")

    if dash.host not in {"127.0.0.1", "localhost", "::1"} and not dash.allowed_hosts:
        console.print("[red]Refusing to bind dashboard to a non-local host without allowedHosts configured.[/red]")
        console.print("Set dashboard.allowedHosts in ~/.kyber/config.json to the hostnames you expect.")
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
    uvicorn.run(app, host=dash.host, port=dash.port, log_level="warning", access_log=False)


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")

skills_app = typer.Typer(help="Manage skills (skills.sh compatible)")
app.add_typer(skills_app, name="skills")


@skills_app.command("list")
def skills_list():
    """List skills from workspace, managed (~/.kyber/skills), and builtin."""
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
    """Install skills into ~/.kyber/skills by cloning a repo and copying SKILL.md directories."""
    from kyber.skillhub.manager import install_from_source

    res = install_from_source(source, skill=skill, replace=replace)
    installed = res.get("installed") or []
    if installed:
        console.print(f"[green]✓[/green] Installed: {', '.join(installed)}")
    else:
        console.print("[yellow]Nothing installed[/yellow] (already present?)")


@skills_app.command("remove")
def skills_remove(name: str = typer.Argument(..., help="Skill directory name under ~/.kyber/skills")):
    """Remove a managed skill from ~/.kyber/skills."""
    from kyber.skillhub.manager import remove_skill

    remove_skill(name)
    console.print(f"[green]✓[/green] Removed: {name}")


@skills_app.command("update-all")
def skills_update_all():
    """Update all managed skill sources recorded in the manifest."""
    from kyber.skillhub.manager import update_all

    res = update_all(replace=True)
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
    from kyber.config.loader import get_data_dir
    from kyber.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
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
    from kyber.config.loader import get_data_dir
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
    
    store_path = get_data_dir() / "cron" / "jobs.json"
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
    from kyber.config.loader import get_data_dir
    from kyber.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
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
    from kyber.config.loader import get_data_dir
    from kyber.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
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
    from kyber.config.loader import get_data_dir
    from kyber.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
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
        chat_prov = config.get_chat_provider_name()
        task_prov = config.get_task_provider_name()
        console.print(f"Chat: {chat_prov or 'default'} / {config.get_chat_model()}")
        console.print(f"Tasks: {task_prov or 'default'} / {config.get_task_model()}")
        
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
    _verify_clamav_setup(system)
    console.print(f"\n{__logo__} ClamAV is ready! Scans will use the clamd daemon for fast malware detection.")
    console.print("[dim]Run [cyan]kyber clamscan --schedule[/cyan] to set up a daily background scan.[/dim]")


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
                for line in lines:
                    stripped = line.strip()
                    if stripped == "Example":
                        new_lines_l.append("# Example")
                        needs_write = True
                        continue
                    if "LocalSocket" in stripped and not stripped.startswith("#"):
                        has_socket = True
                    new_lines_l.append(line)
                if not has_socket:
                    new_lines_l.append("LocalSocket /var/run/clamav/clamd.ctl")
                    needs_write = True
                if needs_write:
                    sp.run(
                        ["sudo", "tee", str(conf_path)],
                        input="\n".join(new_lines_l) + "\n",
                        capture_output=True, text=True,
                    )
                console.print(f"  [green]✓[/green] {conf_path}")
                return

        # No config found — create one
        debian_conf = Path("/etc/clamav/clamd.conf")
        sp.run(
            ["sudo", "tee", str(debian_conf)],
            input=(
                "# Kyber-managed clamd config\n"
                "LocalSocket /var/run/clamav/clamd.ctl\n"
                "DatabaseDirectory /var/lib/clamav\n"
                "User clamav\n"
            ),
            capture_output=True, text=True,
        )
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


def _setup_linux_clamav_services():
    """Enable and start ClamAV systemd services."""
    import subprocess as sp

    console.print("[cyan]Enabling ClamAV services...[/cyan]")

    for svc_name in ["clamav-freshclam", "freshclam"]:
        result = sp.run(["sudo", "systemctl", "enable", svc_name], capture_output=True, text=True)
        if result.returncode == 0:
            sp.run(["sudo", "systemctl", "start", svc_name], capture_output=True, text=True)
            console.print(f"  [green]✓[/green] {svc_name} enabled and started")
            break
    else:
        console.print("  [yellow]⚠[/yellow] Could not enable freshclam service")

    for svc_name in ["clamav-daemon", "clamd@scan", "clamd"]:
        result = sp.run(["sudo", "systemctl", "enable", svc_name], capture_output=True, text=True)
        if result.returncode == 0:
            sp.run(["sudo", "systemctl", "start", svc_name], capture_output=True, text=True)
            console.print(f"  [green]✓[/green] {svc_name} enabled and started")
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


def _verify_clamav_setup(system: str):
    """Verify ClamAV daemon is running and responsive."""
    import shutil
    import subprocess as sp

    console.print("[cyan]Verifying setup...[/cyan]")

    db_dir = _find_clamav_db_dir()
    if db_dir:
        db_files = [f.name for f in db_dir.iterdir() if f.suffix in (".cvd", ".cld")]
        if db_files:
            console.print(f"  [green]✓[/green] Signatures: {', '.join(sorted(db_files))}")
        else:
            console.print("  [yellow]⚠[/yellow] No signature databases — clamd may not start")

    if shutil.which("clamdscan"):
        result = sp.run(["clamdscan", "--ping", "3"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            console.print("  [green]✓[/green] clamd is running and responsive")
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
                break
        else:
            console.print("  [yellow]⚠[/yellow] clamd service not active")


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
