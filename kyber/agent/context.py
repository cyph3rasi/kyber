"""Context builder for assembling agent prompts."""

import base64
import mimetypes
from pathlib import Path
from typing import Any

from kyber.agent.memory import MemoryStore
from kyber.agent.skills import SkillsLoader


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.
    
    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """
    
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        self._task_status_fn: Any = None
    
    def set_task_status_provider(self, fn: Any) -> None:
        """Set a callable that returns current active task status text.
        
        This is typically SubagentManager.get_all_status or similar.
        Keeps ContextBuilder decoupled from the subagent module.
        """
        self._task_status_fn = fn
    
    
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

            # Active background tasks — so the LLM knows what's in flight
            task_status = self._get_active_task_context()
            if task_status:
                parts.append(task_status)

            return "\n\n---\n\n".join(parts)


    def _get_active_task_context(self) -> str | None:
        """Build a context section describing currently running background tasks.
        
        Returns None if no tasks are active or no status provider is set.
        """
        if not self._task_status_fn:
            return None
        try:
            status_text = self._task_status_fn()
        except Exception:
            return None
        if not status_text or "No subagent tasks" in status_text:
            return None
        return (
            "# Active Background Tasks\n\n"
            "The following tasks are currently running in the background. "
            "When the user asks about progress or what's happening, refer to these — "
            "not to older completed tasks from the conversation history.\n\n"
            f"{status_text}"
        )

    def build_meta_system_prompt(self) -> str:
        """
        Build a minimal persona-only system prompt for user-visible meta messages.

        We intentionally avoid including the full identity/tooling instructions
        so these short status updates don't accidentally echo internal rules.
        """
        soul_path = self.workspace / "SOUL.md"
        if soul_path.exists():
            try:
                soul = soul_path.read_text(encoding="utf-8").strip()
            except Exception:
                soul = ""
            if soul:
                return (
                    "Write in the assistant's normal voice as defined below.\n\n"
                    f"{soul}\n\n"
                    "Keep meta updates short, natural, and human."
                )
        return "You are a helpful assistant. Keep meta updates short, natural, and human."
    
    def _get_identity(self) -> str:
        """Get the core identity section."""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        workspace_path = str(self.workspace.expanduser().resolve())
        
        return f"""# kyber 💎

You are kyber, a helpful AI assistant. You have access to tools that allow you to:
- Read, write, and edit files
- Execute shell commands
- Search the web and fetch web pages
- Send messages to users on chat channels
- Spawn subagents for complex background tasks

## Current Time
{now}

## Workspace
Your workspace is at: {workspace_path}
- Memory files: {workspace_path}/memory/MEMORY.md
- Daily notes: {workspace_path}/memory/YYYY-MM-DD.md
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md
- Managed skills: ~/.kyber/skills/{{skill-name}}/SKILL.md

IMPORTANT: When responding to direct questions or conversations, reply directly with your text response.
Only use the 'message' tool when you need to send a message to a specific chat channel (like WhatsApp).
For normal conversation, just respond with text - do not call the message tool.

CRITICAL — TOOL USAGE RULES:
- When asked to edit code, create files, run commands, or make any changes: YOU MUST USE TOOLS. Do not describe or narrate changes — actually call write_file, edit_file, exec, etc.
- Never say "I've updated the file" or "I've made the changes" unless you literally called a tool and saw the result in this conversation.
- If you're unsure what to change, ask. If you know what to change, use the tools to do it.
- For code tasks: read the file first (read_file), make edits (edit_file/write_file), then verify if needed (exec).

CRITICAL — SPAWN DECISION:
Before your first tool call on any user request, decide: can you handle this in 1-2 quick tool calls, or will it require multiple steps (creating files, writing code, running commands, research, installations)?
- If it's complex (3+ tool calls), call the spawn tool IMMEDIATELY and include a brief natural acknowledgment in your text response. The user sees your text right away while the subagent works in the background.
- If it's simple, handle it directly.
- When in doubt, spawn. The user would rather get a quick ack than wait in silence.

Always be helpful, accurate, and concise. When using tools, explain what you're doing.
When remembering something, write to {workspace_path}/memory/MEMORY.md"""

        def _get_system_instructions(self) -> str:
            """Return hardcoded system instructions that ship with kyber.

            These are NOT user-editable — they describe how kyber's subsystems
            work (cron, heartbeat, sessions, tools, guidelines).  Keeping them
            in code means every upgrade delivers the latest version automatically
            without touching the user's workspace files.
            """
            workspace_path = str(self.workspace.expanduser().resolve())
            return f"""# System Instructions

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
    - **spawn** — Spawn a subagent to handle a task in the background. Before your first tool call on any request, decide: can you answer this in one or two quick steps, or will it require multiple tool calls (creating files, running commands, research, installations, etc.)? If it's complex, call spawn FIRST and include a brief natural acknowledgment in your text response — that text is what the user sees immediately. The subagent handles the work and reports back when done.
    - **task_status** — Check the progress of running subagent tasks. Call with no arguments to see all tasks, or pass a task_id for a specific one.

    ## Cron (Scheduled Tasks)

    Kyber has a built-in cron system for recurring or one-off scheduled tasks. Jobs are stored in `~/.kyber/cron/jobs.json`.

    ### Schedule Types
    - **every** — Run at a fixed interval (e.g., every 30 minutes)
    - **cron** — Standard cron expression (e.g., `0 9 * * *` for daily at 9am)
    - **at** — Run once at a specific timestamp

    ### Job Payloads
    - **agent_turn** — Sends a message to the agent (you) to process
    - **system_event** — Triggers a system-level event

    Jobs can optionally deliver their output to a chat channel (Discord, Telegram, etc.).

    To manage cron jobs via CLI:
    ```
    kyber cron add --name "name" --message "message" --cron "expr"
    kyber cron add --name "name" --message "message" --every <seconds>
    kyber cron add --name "name" --message "message" --at "ISO-timestamp" --deliver --to "USER_ID" --channel "CHANNEL"
    kyber cron list
    kyber cron remove <job-id>
    ```

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
    - **Remember things.** Write important information to {workspace_path}/memory/MEMORY.md so you don't forget across sessions.
    - **Use spawn for heavy work.** If a task needs more than 2-3 tool calls, spawn a subagent immediately. Don't start doing the work yourself and then realize it's taking too long — decide upfront. Your text response alongside the spawn call is sent to the user right away, so make it a natural acknowledgment of what you're about to do.
    - **Stay in scope.** Your workspace is your operating area. Be mindful of file paths and permissions."""

    
    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        
        return "\n\n".join(parts) if parts else ""
    
    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build the complete message list for an LLM call.

        Args:
            history: Previous conversation messages.
            current_message: The new user message.
            skill_names: Optional skills to include.
            media: Optional list of local file paths for images/media.

        Returns:
            List of messages including system prompt.
        """
        messages = []

        # System prompt
        system_prompt = self.build_system_prompt(skill_names)
        messages.append({"role": "system", "content": system_prompt})

        # History
        messages.extend(history)

        # Current message (with optional image attachments)
        user_content = self._build_user_content(current_message, media)
        messages.append({"role": "user", "content": user_content})

        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text
        
        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(p.read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        
        if not images:
            return text
        return images + [{"type": "text", "text": text}]
    
    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str
    ) -> list[dict[str, Any]]:
        """
        Add a tool result to the message list.
        
        Args:
            messages: Current message list.
            tool_call_id: ID of the tool call.
            tool_name: Name of the tool.
            result: Tool execution result.
        
        Returns:
            Updated message list.
        """
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result
        })
        return messages
    
    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """
        Add an assistant message to the message list.
        
        Args:
            messages: Current message list.
            content: Message content.
            tool_calls: Optional tool calls.
        
        Returns:
            Updated message list.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        
        if tool_calls:
            msg["tool_calls"] = tool_calls
        
        messages.append(msg)
        return messages
