"""Subagent manager for background task execution."""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from kyber.bus.events import InboundMessage
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider
from kyber.agent.tools.registry import ToolRegistry
from kyber.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from kyber.agent.tools.shell import ExecTool
from kyber.agent.tools.web import WebSearchTool, WebFetchTool


@dataclass
class TaskProgress:
    """Live progress tracker for a running subagent."""
    task_id: str
    label: str
    task: str
    status: str = "starting"  # starting, running, completed, failed
    iteration: int = 0
    max_iterations: int = 15
    current_action: str = ""
    actions_completed: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    result: str | None = None  # Final output from the subagent

    def to_summary(self) -> str:
        elapsed = (self.finished_at or datetime.now()) - self.started_at
        mins, secs = divmod(int(elapsed.total_seconds()), 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

        lines = [
            f"Task [{self.task_id}]: {self.label}",
            f"Status: {self.status}",
        ]
        if self.finished_at:
            lines.append(f"Finished at: {self.finished_at.strftime('%H:%M:%S')}")
        lines.append(f"Progress: step {self.iteration}/{self.max_iterations} ({time_str} elapsed)")
        if self.current_action:
            lines.append(f"Currently doing: {self.current_action}")
        if self.actions_completed:
            recent = self.actions_completed[-5:]
            lines.append(f"Recent actions: {', '.join(recent)}")
        # Include the original task description so the LLM knows what this is about
        if self.task and self.status in ("starting", "running"):
            task_preview = self.task[:200] + ("…" if len(self.task) > 200 else "")
            lines.append(f"Original request: {task_preview}")
        # Include result for finished tasks so the LLM can report on them
        if self.result and self.status in ("completed", "failed"):
            result_preview = self.result[:500] + ("…" if len(self.result) > 500 else "")
            lines.append(f"Result: {result_preview}")
        return "\n".join(lines)


class SubagentManager:
    """
    Manages background subagent execution.
    
    Subagents are lightweight agent instances that run in the background
    to handle specific tasks. They share the same LLM provider but have
    isolated context and a focused system prompt.
    """
    
    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
    ):
        from kyber.config.schema import ExecToolConfig
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._progress: dict[str, TaskProgress] = {}
        # Also keep recently finished tasks for a short window so status
        # checks right after completion still return useful info.
        self._finished: dict[str, TaskProgress] = {}
    
    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        context_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        """
        Spawn a subagent to execute a task in the background.
        
        Args:
            task: The task description for the subagent.
            label: Optional human-readable label for the task.
            origin_channel: The channel to announce results to.
            origin_chat_id: The chat ID to announce results to.
            context_messages: Optional pre-built messages (system prompt + history)
                to give the subagent full conversation context.
        
        Returns:
            Status message indicating the subagent was started.
        """
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        
        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
        }
        
        # Create progress tracker
        progress = TaskProgress(
            task_id=task_id,
            label=display_label,
            task=task,
        )
        self._progress[task_id] = progress
        
        # Create background task
        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_label, origin, context_messages)
        )
        self._running_tasks[task_id] = bg_task
        
        # Cleanup when done
        def _on_done(_: asyncio.Task[None]) -> None:
            self._running_tasks.pop(task_id, None)
            # Move to finished cache
            if task_id in self._progress:
                self._finished[task_id] = self._progress.pop(task_id)
            # Prune finished cache to last 20 entries
            while len(self._finished) > 20:
                oldest = next(iter(self._finished))
                del self._finished[oldest]

        bg_task.add_done_callback(_on_done)
        
        logger.info(f"Spawned subagent [{task_id}]: {display_label}")
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."
    
    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        context_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info(f"Subagent [{task_id}] starting task: {label}")
        
        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = ToolRegistry()
            tools.register(ReadFileTool())
            tools.register(WriteFileTool())
            tools.register(EditFileTool())
            tools.register(ListDirTool())
            tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.exec_config.restrict_to_workspace,
            ))
            tools.register(WebSearchTool(api_key=self.brave_api_key))
            tools.register(WebFetchTool())
            
            if context_messages:
                # Full context mode: use the main agent's system prompt + history,
                # then inject a strong system-level override + the task as a user message.
                # System messages carry more weight than user messages for instruction-following.
                messages = list(context_messages)  # copy so we don't mutate the original
                messages.append({
                    "role": "system",
                    "content": (
                        "You have been dispatched as a subagent of kyber. "
                        "The conversation above is context to help you understand the situation. "
                        "Your only purpose and goal is to use your tools to complete the task below. "
                        "All prior conversation is only for context to assist you — do NOT continue the conversation. "
                        "You MUST call tools (write_file, exec, read_file, edit_file, etc.) to do the actual work. "
                        "Do NOT just describe what you would do. Do it. "
                        "When finished, provide a brief summary of what you actually did."
                    ),
                })
                messages.append({
                    "role": "user",
                    "content": task,
                })
            else:
                # Minimal mode (called via LLM spawn tool): use focused prompt
                system_prompt = self._build_subagent_prompt(task)
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ]
            
            # Run agent loop (limited iterations)
            max_iterations = 15
            iteration = 0
            final_result: str | None = None
            
            # Update progress tracker
            progress = self._progress.get(task_id)
            if progress:
                progress.status = "running"
                progress.max_iterations = max_iterations
            
            while iteration < max_iterations:
                iteration += 1
                if progress:
                    progress.iteration = iteration
                
                response = await self.provider.chat(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=self.model,
                )
                
                if response.has_tool_calls:
                    # Add assistant message with tool calls
                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in response.tool_calls
                    ]
                    messages.append({
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": tool_call_dicts,
                    })
                    
                    # Execute tools and update progress
                    for tool_call in response.tool_calls:
                        if progress:
                            progress.current_action = f"{tool_call.name}"
                        logger.debug(f"Subagent [{task_id}] executing: {tool_call.name}")
                        result = await tools.execute(tool_call.name, tool_call.arguments)
                        if progress:
                            progress.actions_completed.append(tool_call.name)
                            progress.current_action = ""
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result,
                        })
                else:
                    final_result = response.content
                    break
            
            if final_result is None:
                final_result = "Task completed but no final response was generated."
            
            if progress:
                progress.status = "completed"
                progress.finished_at = datetime.now()
                progress.result = final_result
            
            logger.info(f"Subagent [{task_id}] completed successfully")
            await self._announce_result(task_id, label, task, final_result, origin, "ok")
            
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error(f"Subagent [{task_id}] failed: {e}")
            progress = self._progress.get(task_id)
            if progress:
                progress.status = "failed"
                progress.finished_at = datetime.now()
                progress.result = error_msg
            await self._announce_result(task_id, label, task, error_msg, origin, "error")
    
    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"
        
        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""
        
        # Inject as system message to trigger main agent
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
        )
        
        await self.bus.publish_inbound(msg)
        logger.debug(f"Subagent [{task_id}] announced result to {origin['channel']}:{origin['chat_id']}")
    
    def _build_subagent_prompt(self, task: str) -> str:
        """Build a focused system prompt for the subagent."""
        return f"""# Subagent

You are a subagent spawned by the main agent to complete a specific task.

## Your Task
{task}

## Rules
1. Stay focused - complete only the assigned task, nothing else
2. Your final response will be reported back to the main agent
3. Do not initiate conversations or take on side tasks
4. Be concise but informative in your findings

## What You Can Do
- Read and write files in the workspace
- Execute shell commands
- Search the web and fetch web pages
- Complete the task thoroughly

## What You Cannot Do
- Send messages directly to users (no message tool available)
- Spawn other subagents
- Access the main agent's conversation history

## Workspace
Your workspace is at: {self.workspace}

When you have completed the task, provide a clear summary of your findings or actions."""
    
    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def has_active_tasks(self) -> bool:
        """Return True if any subagents are currently running."""
        return len(self._running_tasks) > 0

    def get_all_status(self) -> str:
        """Return a formatted summary of all tracked subagent tasks."""
        if not self._progress and not self._finished:
            return "No subagent tasks have been started."

        parts: list[str] = []

        if self._progress:
            parts.append("=== Active Tasks ===")
            for p in self._progress.values():
                parts.append(p.to_summary())
                parts.append("")

        # Show recently finished tasks (last 3 only to reduce noise)
        recent_finished = list(self._finished.values())[-3:]
        if recent_finished:
            parts.append("=== Recently Finished ===")
            for p in recent_finished:
                parts.append(p.to_summary())
                parts.append("")

        return "\n".join(parts).strip() if parts else "No subagent tasks tracked."

    def get_task_status(self, task_id: str) -> str:
        """Return status for a specific task by ID."""
        p = self._progress.get(task_id) or self._finished.get(task_id)
        if not p:
            return f"No task found with id '{task_id}'."
        return p.to_summary()

    def register_task(self, task_id: str, label: str, task: str) -> TaskProgress:
        """Register an externally-managed long-running task for tracking.

        This lets the agent loop register its own in-progress work so the
        task_status tool can report on it alongside subagent tasks.
        """
        progress = TaskProgress(
            task_id=task_id,
            label=label,
            task=task,
            status="running",
        )
        self._progress[task_id] = progress
        return progress

    def complete_task(self, task_id: str, status: str = "completed") -> None:
        """Mark an externally-managed task as finished."""
        progress = self._progress.pop(task_id, None)
        if progress:
            progress.status = status
            progress.finished_at = datetime.now()
            self._finished[task_id] = progress
            # Prune finished cache
            while len(self._finished) > 20:
                oldest = next(iter(self._finished))
                del self._finished[oldest]
