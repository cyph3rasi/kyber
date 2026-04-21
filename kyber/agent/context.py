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

        # Kyber Network — tell the agent it exists and who's paired.
        # Without this, the LLM has no idea notebook_* tools are for
        # cross-machine state and doesn't know it has peers at all.
        network = self._get_network_context()
        if network:
            parts.append(network)

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
        """Get the core identity section (kept tight to maximize cache hit area)."""
        from kyber.utils.helpers import current_datetime_str
        now = current_datetime_str(self.timezone)
        workspace_path = str(self.workspace.expanduser().resolve())

        return (
            "# kyber 💎\n"
            "You are kyber, a helpful AI assistant with direct tool access.\n"
            f"Current time: {now}\n"
            f"Workspace: {workspace_path}\n\n"
            "Be helpful, accurate, and concise. Write durable notes to "
            "`memory/MEMORY.md` (agent notes) or `USER.md` (user profile). "
            "There is exactly one USER.md — never create `memory/USER.md`."
        )

    def _get_system_instructions(self) -> str:
        """Compact system instructions.

        Intentionally terse — every token here is re-sent on every LLM call
        in the loop, and modern models don't need hand-holding about how
        tool calling works.
        """
        workspace_path = str(self.workspace.expanduser().resolve())
        return (
            "# System Instructions\n"
            "You have direct tool access. Call tools, read results, iterate, "
            "then respond. Be concise; don't narrate your plan unless asked.\n\n"
            "## Guidelines\n"
            "- Shell commands must be non-interactive (`-y`, `sudo -n`, `ssh -o BatchMode=yes`).\n"
            "- Don't re-run discovery commands (`list_dir`, `read_file`, broad `exec`) "
            "on paths already inspected this turn unless the user asked to refresh.\n"
            "- On failure, try alternatives or explain what went wrong.\n"
            f"- Persist notes to `{workspace_path}/memory/MEMORY.md`; user facts to `{workspace_path}/USER.md`.\n"
            f"- New/edited skills live under `{workspace_path}/skills/`, not `~/.kyber/skills/`.\n\n"
            "## Workspace layout\n"
            "AGENTS.md SOUL.md USER.md IDENTITY.md TOOLS.md HEARTBEAT.md "
            "memory/MEMORY.md memory/YYYY-MM-DD.md skills/\n\n"
            "## Cron\n"
            "Built-in scheduler at `~/.kyber/cron/jobs.json`. Schedule jobs by "
            "calling your tools directly — don't write standalone Python "
            "scripts or request separate API keys.\n\n"
            "## Sessions\n"
            "Keyed by `channel:chat_id`. History persists across messages."
        )

    def _get_network_context(self) -> str:
        """Describe this machine's Kyber Network state + shared tools.

        Injected into the system prompt on every turn so the LLM knows:

        * Whether it's standalone / host / spoke.
        * What other machines it's paired with and their display names.
        * That ``notebook_*`` tools talk to a shared store every paired
          Kyber can see — useful for cross-machine memory and handoff.

        Always returns *something* (even in standalone mode) so the agent
        at least knows these tools exist and what they're for.
        """
        try:
            from kyber.network.state import (
                ROLE_HOST,
                ROLE_SPOKE,
                ROLE_STANDALONE,
                load_state,
            )
        except Exception:
            return ""

        try:
            state = load_state()
        except Exception:
            return ""

        role = (state.role or ROLE_STANDALONE).strip().lower()

        lines = ["# Kyber Network"]
        lines.append(
            f"This machine is **{state.name or '(unnamed)'}** "
            f"(peer id `{state.peer_id[:12]}…`) in role **{role}**."
        )

        if role == ROLE_HOST:
            if state.paired_peers:
                names = ", ".join(f"**{p.name}**" for p in state.paired_peers)
                lines.append(
                    f"You are the network host. Paired spokes: {names}. "
                    "Each can read and write the shared notebook."
                )
            else:
                lines.append(
                    "You are the network host, but no spokes have paired yet. "
                    "Share a pairing code via `kyber network pair` to add machines."
                )
        elif role == ROLE_SPOKE and state.host_peer is not None:
            lines.append(
                f"You are a spoke paired with **{state.host_peer.name}** "
                f"at `{state.host_url}`. You share that host's notebook."
            )
        else:
            lines.append(
                "No other Kyber instances are paired with this one yet. "
                "The notebook tools will still work once you're paired."
            )

        lines.append("")
        lines.append(
            "## Doing things on other machines — use these, not the notebook"
        )
        lines.append(
            "- `list_network_peers()` — see who's on the network. Use this "
            "first when you don't know peer names."
        )
        lines.append(
            "- `exec_on(peer_name, command)` — run a shell command on a paired "
            "machine. Target enforces its own allowlist via "
            "`network.exposedTools` in config."
        )
        lines.append(
            "- `read_file_on(peer_name, path)` — read a file on a paired machine."
        )
        lines.append(
            "- `list_dir_on(peer_name, path)` — list a directory on a paired machine."
        )
        lines.append(
            "- `remote_invoke(peer_name, tool_name, params)` — escape hatch "
            "for any other allow-listed tool (e.g. `web_fetch` on a VPS "
            "with a different external IP)."
        )
        lines.append("")
        lines.append(
            "## Sharing state across machines — use the notebook"
        )
        lines.append(
            "- `notebook_write(key, value, tags=[], replace=False)` — save a note."
        )
        lines.append(
            "- `notebook_read(key, limit=1)` — fetch the latest entries for a key."
        )
        lines.append(
            "- `notebook_list(tag=None, limit=20)` — browse recent entries."
        )
        lines.append(
            "- `notebook_search(query)` — substring search over keys/values/tags."
        )
        lines.append("")
        lines.append(
            "**Critical routing rule**: if the user asks you to DO something "
            "on another machine (run a command, check disk, read a file, "
            "restart a service) — USE `exec_on` / `read_file_on` / "
            "`list_dir_on` / `remote_invoke`. Do NOT write a note into the "
            "notebook and call it done. The notebook is for sharing facts "
            "and context across machines, not for issuing commands."
        )

        return "\n".join(lines)

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
