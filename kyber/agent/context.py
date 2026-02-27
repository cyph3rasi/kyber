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
        Build the system prompt from identity, instructions,
        bootstrap files, memory, and skills.
        """
        parts = []

        # Core identity
        parts.append(self._get_identity())

        # System instructions (direct tool-calling architecture)
        parts.append(self._get_system_instructions())

        # User-editable bootstrap files (AGENTS.md, SOUL.md, USER.md, etc.)
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        # Memory context
        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        # Skills - progressive loading
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

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
            "You are kyber, a helpful AI assistant with direct access to tools.\n"
            "You can read/write files, execute shell commands, search the web, "
            "and send messages — all by calling your tools directly.\n\n"
            "## Capabilities\n"
            "- Read, write, and edit files\n"
            "- Execute shell commands\n"
            "- Search the web and fetch web pages\n"
            "- Send messages to users on chat channels\n\n"
            f"## Current Time\n{now}\n\n"
            f"## Workspace\n"
            f"Your workspace is at: {workspace_path}\n"
            f"- Agent instructions: {workspace_path}/AGENTS.md\n"
            f"- Persona notes: {workspace_path}/SOUL.md\n"
            f"- User profile: {workspace_path}/USER.md (single source of truth)\n"
            f"- Identity constraints: {workspace_path}/IDENTITY.md\n"
            f"- Tool guidance: {workspace_path}/TOOLS.md\n"
            f"- Heartbeat tasks: {workspace_path}/HEARTBEAT.md\n"
            f"- Memory files: {workspace_path}/memory/MEMORY.md\n"
            f"- Daily notes: {workspace_path}/memory/YYYY-MM-DD.md\n"
            f"- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md\n"
            "\n"
            "Always be helpful, accurate, and concise.\n"
            f"When remembering something, write to {workspace_path}/memory/MEMORY.md or {workspace_path}/USER.md.\n"
            "Never create a second user profile file like memory/USER.md."
        )

    def _get_system_instructions(self) -> str:
        """Return system instructions for the direct tool-calling architecture.
        
        These describe how the agent works with direct tool calling (not intent-based).
        """
        workspace_path = str(self.workspace.expanduser().resolve())
        return (
            "# System Instructions\n\n"
            "## Architecture — Direct Tool Calling\n\n"
            "You have direct access to tools. When you need to take action (read a file, "
            "run a command, search the web, etc.), call the appropriate tool directly. "
            "The system will execute it and return the result, then you can continue "
            "your reasoning or call more tools as needed.\n\n"
            "### How It Works\n"
            "1. User sends a message\n"
            "2. You decide what to do — respond directly or call tools\n"
            "3. If you call tools, you get results back and can call more or respond\n"
            "4. When you're done, send your final response\n\n"
            "### Available Tools\n"
            "- **read_file** — Read a file's contents\n"
            "- **write_file** — Write content to a file (creates parent dirs)\n"
            "- **edit_file** — Replace specific text in a file (old → new)\n"
            "- **list_dir** — List directory contents\n"
            "- **exec** — Execute shell commands (timeout: 60s, dangerous commands blocked)\n"
            "- **web_search** — Search the web via Brave Search\n"
            "- **web_fetch** — Fetch and extract content from a URL\n"
            "- **message** — Send a message to a chat channel/user\n\n"
            "- **mcp_list_servers / mcp_list_tools / mcp_call_tool** — Discover and use configured MCP servers\n\n"
            "### Guidelines\n"
            "- **Be direct.** Call tools when needed, don't ask permission first.\n"
            "- **Be concise.** Respect the user's time.\n"
            "- **Chain tools.** Multi-step tasks are fine — read, modify, verify.\n"
            "- **Handle errors.** If a tool fails, try alternatives or explain what went wrong.\n"
            f"- **Remember things.** Use {workspace_path}/memory/MEMORY.md for agent notes and {workspace_path}/USER.md for user profile facts.\n"
            "- **Single USER file.** There is exactly one user profile file: USER.md at workspace root. Do not create memory/USER.md.\n"
            f"- **Skill location rule.** Skills you create/edit should live in {workspace_path}/skills/, not ~/.kyber/skills.\n"
            "- **Stay in scope.** Be mindful of file paths and permissions.\n\n"
            "### Strict Efficiency Rules\n"
            "- Treat repeated discovery calls as a bug unless a refresh is actually needed.\n"
            "- Before calling `list_dir`, `read_file`, or broad `exec` discovery commands, check recent tool outputs and prior messages first.\n"
            "- Do NOT re-run the same `list_dir`/`read_file` on the same path in the same session unless one of these is true: user asked for refresh, files likely changed, or prior output was missing/ambiguous.\n"
            "- If you must repeat a discovery call, state the refresh reason briefly before doing it.\n"
            "- Prefer incremental inspection over re-discovery (targeted reads/grep/diff instead of repeating broad scans).\n\n"
            "### Workspace Layout\n"
            f"- `{workspace_path}/AGENTS.md` custom instructions\n"
            f"- `{workspace_path}/SOUL.md` persona notes\n"
            f"- `{workspace_path}/USER.md` user profile (only one)\n"
            f"- `{workspace_path}/IDENTITY.md` identity constraints\n"
            f"- `{workspace_path}/TOOLS.md` tool guidance\n"
            f"- `{workspace_path}/HEARTBEAT.md` recurring heartbeat tasks\n"
            f"- `{workspace_path}/memory/MEMORY.md` long-term agent notes\n"
            f"- `{workspace_path}/memory/YYYY-MM-DD.md` daily notes\n"
            f"- `{workspace_path}/skills/` workspace skills\n\n"
            "## Cron (Scheduled Tasks)\n\n"
            "Kyber has a built-in cron system for recurring or one-off scheduled tasks.\n"
            "Jobs are stored in `~/.kyber/cron/jobs.json`.\n"
            "For scheduled/cron tasks: execute as kyber directly using built-in tools "
            "and the configured provider/model.\n"
            "Do NOT create standalone Python scripts for LLM reasoning.\n"
            "Do NOT request separate API keys.\n\n"
            "## Sessions\n\n"
            "Each conversation is tracked as a session, keyed by `channel:chat_id`. "
            "History persists across messages within a session."
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
