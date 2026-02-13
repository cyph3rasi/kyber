"""Context builder for assembling agent prompts."""

from pathlib import Path

from kyber.agent.memory import MemoryStore
from kyber.agent.skills import SkillsLoader


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.
    
    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """
    
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
    
    def __init__(self, workspace: Path, timezone: str | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        # Cache for bootstrap files — avoids re-reading disk on every message.
        # Maps filename → (mtime, content). Invalidated when mtime changes.
        self._bootstrap_cache: dict[str, tuple[float, str]] = {}

    def build_system_prompt(self, skill_names: list[str] | None = None) -> str:
        """
        Build the system prompt from identity, hardcoded instructions,
        bootstrap files, memory, and skills.
        """
        parts = []

        # Core identity (who am I, workspace paths, critical rules)
        parts.append(self._get_identity())

        # Hardcoded system instructions (tools, cron, heartbeat, guidelines)
        # These ship with the code and are always up-to-date on upgrade.
        parts.append(self._get_system_instructions())

        # User-editable bootstrap files (AGENTS.md, SOUL.md, USER.md, etc.)
        # These are additive — custom instructions, personality, user prefs.
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        # Memory context
        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        # Skills - progressive loading
        # 1. Always-loaded skills: include full content
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        # 2. Available skills: only show summary (agent uses read_file to load)
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self) -> str:
        """Get the core identity section."""
        from kyber.utils.helpers import current_datetime_str
        now = current_datetime_str(self.timezone)
        workspace_path = str(self.workspace.expanduser().resolve())

        return (
            "# kyber 💎\n\n"
            "You are kyber, a helpful AI assistant. You live inside a structured agent architecture\n"
            "where you declare intent and the system executes on your behalf.\n\n"
            "## Capabilities\n"
            "- Read, write, and edit files (via background workers)\n"
            "- Execute shell commands\n"
            "- Search the web and fetch web pages\n"
            "- Send messages to users on chat channels\n"
            "- Spawn background tasks for complex, multi-step work\n\n"
            f"## Current Time\n{now}\n\n"
            f"## Workspace\n"
            f"Your workspace is at: {workspace_path}\n"
            f"- Memory files: {workspace_path}/memory/MEMORY.md\n"
            f"- Daily notes: {workspace_path}/memory/YYYY-MM-DD.md\n"
            f"- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md\n"
            "- Managed skills: ~/.kyber/skills/{skill-name}/SKILL.md\n\n"
            "Always be helpful, accurate, and concise.\n"
            f"When remembering something, write to {workspace_path}/memory/MEMORY.md"
        )

    def _get_system_instructions(self) -> str:
        """Return hardcoded system instructions that ship with kyber.

        These are NOT user-editable — they describe how kyber's subsystems
        work (cron, heartbeat, sessions, intent architecture, guidelines).
        Keeping them in code means every upgrade delivers the latest version
        automatically without touching the user's workspace files.
        """
        workspace_path = str(self.workspace.expanduser().resolve())
        return (
            "# System Instructions\n\n"
            "## Architecture — Intent-Based Execution\n\n"
            "You operate inside a structured orchestrator. You do NOT call tools directly during\n"
            "conversation. Instead, you declare your **intent** via the `respond` tool, and the\n"
            "system handles execution through background workers.\n\n"
            "### The `respond` Tool\n"
            "When you need to request action, use the `respond` tool. It accepts:\n"
            '- **message** — Your natural-language reply to the user.\n'
            '- **intent.action** — What the system should do:\n'
            '  - `"spawn_task"` — Kick off a background worker to do real work (create files, run commands, etc.). Provide `task_description` (detailed) and `task_label` (short).\n'
            '  - `"check_status"` — Look up progress on running tasks. Optionally provide `task_ref`.\n'
            '  - `"cancel_task"` — Cancel a running task by `task_ref`.\n'
            '  - `"none"` — Pure conversation, no system action needed.\n\n'
            "### When to Use `respond`\n"
            "Only use the `respond` tool when you need the system to DO something:\n"
            '- When user asks you to create, build, fix, write, install, research, etc.\n'
            "- When checking or canceling tasks\n"
            "- When user explicitly asks for an action\n\n"
            "For pure conversation (questions, explanations, thoughts), just respond naturally "
            "without calling any tools. Keep it simple and direct.\n\n"
            "### When to Spawn\n"
            "If the user asks you to DO something (create, build, fix, write, install, research, etc.),\n"
            'set `intent.action` to `"spawn_task"`. The system will dispatch a background worker that\n'
            "has access to file, shell, and web tools. You don't need to do the work yourself — just\n"
            "describe what needs to happen in `task_description` and give the user a natural acknowledgment.\n\n"
            "### Task References\n"
            "When a task is spawned, the system generates internal reference codes (⚡/✅ tokens)\n"
            "for tracking and the dashboard API. These refs are not shown in chat; do NOT fabricate\n"
            "or mention references in user-visible messages.\n\n"
            "### Honesty Rule\n"
            "Never claim you have started, are working on, or have completed an action unless your\n"
            "intent actually declares it. The system validates this — if your message says \"I'll get\n"
            'on that" but your intent is `"none"`, you will be asked to correct yourself.\n\n'
            "## Worker Tools (Background)\n\n"
            "Background workers have access to these tools. You don't call them directly, but you\n"
            "should know what's available so you can write good task descriptions:\n\n"
            "### File Operations\n"
            "- **read_file** — Read a file's contents.\n"
            "- **write_file** — Write content to a file. Creates parent directories automatically.\n"
            "- **edit_file** — Replace specific text in a file (old_text → new_text). Must match exactly.\n"
            "- **list_dir** — List directory contents.\n\n"
            "### Shell\n"
            "- **exec** — Execute shell commands in the workspace directory. Timeout: 60s. Dangerous commands are blocked.\n\n"
            "### Web\n"
            "- **web_search** — Search the web via Brave Search.\n"
            "- **web_fetch** — Fetch and extract content from a URL.\n\n"
            "### Communication\n"
            "- **message** — Send a message to a specific chat channel/user. Only for proactive outreach, not normal replies.\n\n"
            "## Cron (Scheduled Tasks)\n\n"
            "Kyber has a built-in cron system for recurring or one-off scheduled tasks.\n"
            "Jobs are stored in `~/.kyber/cron/jobs.json`.\n\n"
            "### Schedule Types\n"
            "- **every** — Fixed interval (e.g., every 30 minutes)\n"
            "- **cron** — Standard cron expression (e.g., `0 9 * * *` for daily at 9am)\n"
            "- **at** — One-shot at a specific timestamp\n\n"
            "### Job Payloads\n"
            "- **agent_turn** — Sends a message to you to process\n"
            "- **system_event** — Triggers a system-level event\n\n"
            "Jobs can optionally deliver output to a chat channel (Discord, Telegram, etc.).\n\n"
            "CLI management:\n"
            "```\n"
            'kyber cron add --name "name" --message "message" --cron "expr"\n'
            'kyber cron add --name "name" --message "message" --every <seconds>\n'
            'kyber cron add --name "name" --message "message" --at "ISO-timestamp" --deliver --to "USER_ID" --channel "CHANNEL"\n'
            "kyber cron list\n"
            "kyber cron remove <job-id>\n"
            "```\n\n"
            "## Heartbeat\n\n"
            "Kyber runs a heartbeat service (default: every 30 minutes) that wakes you up to check\n"
            "`HEARTBEAT.md` in your workspace. If it has actionable content, execute it. If nothing\n"
            "needs attention, respond with `HEARTBEAT_OK`.\n\n"
            "## Sessions\n\n"
            "Each conversation is tracked as a session, keyed by `channel:chat_id`. History persists\n"
            "across messages within a session, stored as JSONL in `~/.kyber/sessions/`.\n\n"
            "## Guidelines\n\n"
            "- **Declare intent honestly.** If you're going to do work, set `spawn_task`. If it's just chat, set `none`. Never mismatch.\n"
            "- **Write good task descriptions.** Workers are autonomous — give them enough detail to succeed without follow-up.\n"
            "- **Be concise.** Respect the user's time. Answer directly, explain briefly.\n"
            "- **Ask when unsure.** If a request is ambiguous, clarify before spawning.\n"
            f"- **Remember things.** Write important info to {workspace_path}/memory/MEMORY.md so you don't forget across sessions.\n"
            "- **Spawn for heavy work.** If a request needs file edits, commands, research, or multiple steps — spawn it. Don't try to narrate the work yourself.\n"
            "- **Stay in scope.** Your workspace is your operating area. Be mindful of file paths and permissions."
        )

    
    def _load_bootstrap_files(self) -> str:
        """Load bootstrap files from workspace, with mtime-based caching."""
        parts = []
        
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                try:
                    mtime = file_path.stat().st_mtime
                    cached = self._bootstrap_cache.get(filename)
                    if cached and cached[0] == mtime:
                        content = cached[1]
                    else:
                        content = file_path.read_text(encoding="utf-8")
                        self._bootstrap_cache[filename] = (mtime, content)
                    parts.append(f"## {filename}\n\n{content}")
                except Exception:
                    continue
        
        return "\n\n".join(parts) if parts else ""
