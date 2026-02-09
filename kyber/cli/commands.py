"""CLI commands for kyber."""

import asyncio
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
        console.print("Try reinstalling: pip install --force-reinstall kyber")
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


if __name__ == "__main__":
    app()
