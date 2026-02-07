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
        Build the system prompt from bootstrap files, memory, and skills.
        
        Args:
            skill_names: Optional list of skills to include.
        
        Returns:
            Complete system prompt.
        """
        parts = []
        
        # Core identity
        parts.append(self._get_identity())
        
        # Bootstrap files
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
