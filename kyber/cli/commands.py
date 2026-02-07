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


@app.command()
def onboard():
    """Initialize kyber configuration and workspace."""
    from kyber.config.loader import get_config_path, save_config
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
    
    # Create workspace
    workspace = get_workspace_path()
    console.print(f"[green]✓[/green] Created workspace at {workspace}")
    
    # Create default bootstrap files
    _create_workspace_templates(workspace)
    
    console.print(f"\n{__logo__} kyber is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.kyber/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print("  2. Chat: [cyan]kyber agent -m \"Hello!\"[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/kyber#-chat-apps[/dim]")




def _create_workspace_templates(workspace: Path):
    """Create default workspace template files."""
    templates = {
        "AGENTS.md": """# Agent Instructions

You are kyber 💎, a personal AI assistant running as a persistent service. You live on the user's machine (or VPS) and can be reached from Discord, Telegram, WhatsApp, Feishu, or the command line. You handle multiple conversations concurrently and can run long tasks in the background without blocking.

## Your Workspace

Your workspace is the folder you operate in (default: `~/.kyber/workspace/`). This is your home base. Everything you need is here or accessible from here.

### Bootstrap Files (loaded into your context automatically)
- **AGENTS.md** — These instructions (this file)
- **SOUL.md** — Your personality, voice, and values
- **USER.md** — Information about the user (preferences, timezone, etc.)
- **TOOLS.md** — Additional tool-specific instructions (optional)
- **IDENTITY.md** — Extended identity configuration (optional)

### Memory System
- **memory/MEMORY.md** — Long-term memory. Write important facts, user preferences, and things to remember here. This persists across sessions.
- **memory/YYYY-MM-DD.md** — Daily notes. Automatically organized by date. Use these for session-specific context, task logs, or daily observations.

When you learn something important about the user or need to remember something, write it to MEMORY.md. For transient notes, use today's daily file.

### Skills Directory
- **skills/{skill-name}/SKILL.md** — Workspace-level skills (highest priority)
- **~/.kyber/skills/{skill-name}/SKILL.md** — User-installed skills
- Built-in skills ship with kyber (lowest priority)

Skills extend your capabilities. When a skill is available, read its SKILL.md to learn how to use it. Skills with `always: true` in their metadata are loaded into your context automatically.

## Tools

You have access to these tools. Use them — don't just describe what you would do.

### File Operations
- **read_file** — Read a file's contents. Always read before editing.
- **write_file** — Write content to a file. Creates parent directories automatically.
- **edit_file** — Replace specific text in a file (old_text → new_text). The old_text must match exactly and appear only once.
- **list_dir** — List directory contents.

### Shell
- **exec** — Execute shell commands. Runs in your workspace directory by default. Has a timeout (default 60s) and blocks dangerous commands (rm -rf, format, etc.).

### Web
- **web_search** — Search the web using Brave Search. Returns titles, URLs, and snippets.
- **web_fetch** — Fetch and extract content from a URL.

### Communication
- **message** — Send a message to a specific chat channel and user. Only use this when you need to proactively reach out to a channel. For normal conversation replies, just respond with text.

### Background Tasks
- **spawn** — Spawn a subagent to handle a task in the background. **Before your first tool call on any request, decide: can you answer this in one or two quick steps, or will it require multiple tool calls (creating files, running commands, research, installations, etc.)?** If it's complex, call spawn FIRST and include a brief natural acknowledgment in your text response — that text is what the user sees immediately. The subagent handles the work and reports back when done.
- **task_status** — Check the progress of running subagent tasks. Call with no arguments to see all tasks, or pass a task_id for a specific one.

## Cron (Scheduled Tasks)

Kyber has a built-in cron system for recurring or one-off scheduled tasks. Jobs are stored in `~/.kyber/cron.json`.

### Schedule Types
- **every** — Run at a fixed interval (e.g., every 30 minutes)
- **cron** — Standard cron expression (e.g., `0 9 * * *` for daily at 9am)
- **at** — Run once at a specific timestamp

### Job Payloads
- **agent_turn** — Sends a message to the agent (you) to process
- **system_event** — Triggers a system-level event

Jobs can optionally deliver their output to a chat channel (Discord, Telegram, etc.).

## Heartbeat

Kyber runs a heartbeat service that periodically wakes you up (default: every 30 minutes) to check `HEARTBEAT.md` in your workspace. If the file has actionable content (tasks, reminders, instructions), you execute them. If nothing needs attention, respond with `HEARTBEAT_OK`.

Use HEARTBEAT.md for recurring checks, monitoring tasks, or anything you should periodically attend to.

## Sessions

Each conversation is tracked as a session, keyed by `channel:chat_id`. Your conversation history persists across messages within a session, stored as JSONL files in `~/.kyber/sessions/`.

## Guidelines

- **Use tools, don't narrate.** When asked to edit code, create files, or run commands, actually call the tools. Never say "I've updated the file" without having called write_file or edit_file.
- **Read before editing.** Always read_file before using edit_file to ensure your old_text matches exactly.
- **Be concise.** Respect the user's time. Answer directly, explain briefly.
- **Ask when unsure.** If a request is ambiguous, clarify before acting.
- **Remember things.** Write important information to memory/MEMORY.md so you don't forget across sessions.
- **Use spawn for heavy work.** If a task needs more than 2-3 tool calls, spawn a subagent immediately. Don't start doing the work yourself and then realize it's taking too long — decide upfront. Your text response alongside the spawn call is sent to the user right away, so make it a natural acknowledgment of what you're about to do.
- **Stay in scope.** Your workspace is your operating area. Be mindful of file paths and permissions.
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


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the kyber gateway."""
    from kyber.config.loader import load_config, get_data_dir
    from kyber.bus.queue import MessageBus
    from kyber.providers.litellm_provider import LiteLLMProvider
    from kyber.agent.loop import AgentLoop
    from kyber.channels.manager import ChannelManager
    from kyber.cron.service import CronService
    from kyber.cron.types import CronJob
    from kyber.heartbeat.service import HeartbeatService
    
    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)
    
    console.print(f"{__logo__} Starting kyber gateway on port {port}...")
    
    config = load_config()
    
    # Create components
    bus = MessageBus()
    
    # Create provider (supports OpenRouter, Anthropic, OpenAI, Bedrock)
    api_key = config.get_api_key()
    api_base = config.get_api_base()
    provider_name = (config.agents.defaults.provider or "").strip().lower() or config.get_provider_name()
    model = config.agents.defaults.model
    is_bedrock = model.startswith("bedrock/")

    if provider_name and not api_key and not is_bedrock:
        console.print(f"[red]Error: No API key configured for provider {provider_name}.[/red]")
        console.print(f"Set one in ~/.kyber/config.json under providers.{provider_name}.apiKey")
        raise typer.Exit(1)

    if not api_key and not is_bedrock:
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.kyber/config.json under providers.openrouter.apiKey")
        raise typer.Exit(1)
    
    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=config.agents.defaults.model,
        provider_name=provider_name,
    )
    
    # Create agent
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        brave_api_key=config.tools.web.search.api_key or None,
        search_max_results=config.tools.web.search.max_results,
        exec_config=config.tools.exec,
    )
    
    # Create cron service
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        response = await agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}"
        )
        # Optionally deliver to channel
        if job.payload.deliver and job.payload.to:
            from kyber.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "whatsapp",
                chat_id=job.payload.to,
                content=response or ""
            ))
        return response
    
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path, on_job=on_cron_job)
    
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
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
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
    from kyber.providers.litellm_provider import LiteLLMProvider
    from kyber.agent.loop import AgentLoop
    
    config = load_config()
    
    api_key = config.get_api_key()
    api_base = config.get_api_base()
    provider_name = (config.agents.defaults.provider or "").strip().lower() or config.get_provider_name()
    model = config.agents.defaults.model
    is_bedrock = model.startswith("bedrock/")

    if provider_name and not api_key and not is_bedrock:
        console.print(f"[red]Error: No API key configured for provider {provider_name}.[/red]")
        console.print(f"Set one in ~/.kyber/config.json under providers.{provider_name}.apiKey")
        raise typer.Exit(1)

    if not api_key and not is_bedrock:
        console.print("[red]Error: No API key configured.[/red]")
        raise typer.Exit(1)

    bus = MessageBus()
    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=config.agents.defaults.model,
        provider_name=provider_name,
    )
    
    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
    )
    
    if message:
        # Single message mode
        async def run_once():
            response = await agent_loop.process_direct(message, session_id)
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
                    
                    response = await agent_loop.process_direct(user_input, session_id)
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
    url = f"http://{dash.host}:{dash.port}"
    console.print(f"{__logo__} Kyber dashboard running at {url}")
    if show_token:
        console.print(f"  Token: [bold]{dash.auth_token}[/bold]")
    else:
        masked = dash.auth_token[:6] + "…" + dash.auth_token[-4:]
        console.print(f"  Token: [dim]{masked}[/dim]  (run with --show-token to reveal)")
    console.print(f"  Open:  {url}")
    uvicorn.run(app, host=dash.host, port=dash.port, log_level="info")


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


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

    # Feishu
    fs = config.channels.feishu
    fs_config = f"app_id: {fs.app_id}" if fs.app_id else "[dim]not configured[/dim]"
    table.add_row(
        "Feishu",
        "✓" if fs.enabled else "✗",
        fs_config
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
    from kyber.config.loader import load_config, get_config_path

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} kyber Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        console.print(f"Model: {config.agents.defaults.model}")
        
        # Check API keys
        has_openrouter = bool(config.providers.openrouter.api_key)
        has_anthropic = bool(config.providers.anthropic.api_key)
        has_openai = bool(config.providers.openai.api_key)
        has_gemini = bool(config.providers.gemini.api_key)
        has_vllm = bool(config.providers.vllm.api_base)
        
        console.print(f"OpenRouter API: {'[green]✓[/green]' if has_openrouter else '[dim]not set[/dim]'}")
        console.print(f"Anthropic API: {'[green]✓[/green]' if has_anthropic else '[dim]not set[/dim]'}")
        console.print(f"OpenAI API: {'[green]✓[/green]' if has_openai else '[dim]not set[/dim]'}")
        console.print(f"Gemini API: {'[green]✓[/green]' if has_gemini else '[dim]not set[/dim]'}")
        vllm_status = f"[green]✓ {config.providers.vllm.api_base}[/green]" if has_vllm else "[dim]not set[/dim]"
        console.print(f"vLLM/Local: {vllm_status}")


if __name__ == "__main__":
    app()
