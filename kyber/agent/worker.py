"""
Worker: Executes tasks in the background with guaranteed completion.

Workers run async, emit progress events, and always complete (success or failure).
The completion queue guarantees the user gets notified.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from kyber.agent.task_registry import Task, TaskRegistry, TaskStatus
from kyber.agent.skills import SkillsLoader
from kyber.agent.tools.registry import ToolRegistry
from kyber.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from kyber.agent.tools.shell import ExecTool
from kyber.agent.tools.web import WebSearchTool, WebFetchTool
from kyber.meta_messages import looks_like_prompt_leak
from kyber.providers.base import LLMProvider


class Worker:
    """
    Executes a single task with tools.
    
    Guarantees:
    - Always completes (success, failure, or timeout)
    - Emits progress updates
    - Pushes to completion queue when done
    """
    
    def __init__(
        self,
        task: Task,
        provider: LLMProvider,
        workspace: Path,
        registry: TaskRegistry,
        completion_queue: asyncio.Queue,
        persona_prompt: str,
        model: str | None = None,
        brave_api_key: str | None = None,
        exec_timeout: int = 60,
        timezone: str | None = None,
    ):
        self.task = task
        self.provider = provider
        self.workspace = workspace
        self.registry = registry
        self.completion_queue = completion_queue
        self.persona_prompt = persona_prompt
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.exec_timeout = exec_timeout
        self.timezone = timezone

        self.tools = self._build_tools()
    
    def _build_tools(self) -> ToolRegistry:
        """Build the tool registry for this worker."""
        tools = ToolRegistry()
        tools.register(ReadFileTool())
        tools.register(WriteFileTool())
        tools.register(EditFileTool())
        tools.register(ListDirTool())
        tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_timeout,
        ))
        tools.register(WebSearchTool(api_key=self.brave_api_key))
        tools.register(WebFetchTool())
        return tools
    
    def _build_system_prompt(self) -> str:
        """Build system prompt that embodies the bot's persona while executing tasks."""
        from kyber.utils.helpers import current_datetime_str

        # Workers run without the orchestrator's "intent" wrapper, so we include
        # user/workspace bootstrap + skills here (but NOT the orchestrator's tool-policy rules).
        bootstrap_files = ["AGENTS.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
        bootstrap_parts: list[str] = []
        for fn in bootstrap_files:
            p = self.workspace / fn
            if p.exists():
                try:
                    bootstrap_parts.append(f"## {fn}\n\n{p.read_text(encoding='utf-8')}")
                except Exception:
                    continue
        bootstrap = "\n\n---\n\n".join(bootstrap_parts).strip()

        skills = SkillsLoader(self.workspace)
        always = skills.get_always_skills()
        always_content = skills.load_skills_for_context(always) if always else ""
        skills_summary = skills.build_skills_summary()
        skills_block = ""
        if always_content or skills_summary:
            skills_block = (
                "## Skills\n\n"
                "Skills are markdown playbooks. If your task mentions a skill by name, read its SKILL.md and follow it.\n"
                f"- Workspace skills: {self.workspace}/skills/<skill>/SKILL.md\n"
                "- Managed skills: ~/.kyber/skills/<skill>/SKILL.md\n\n"
            )
            if always_content:
                skills_block += f"### Active Skills\n\n{always_content}\n\n"
            if skills_summary:
                skills_block += f"### Available Skills\n\n{skills_summary}\n"

        return f"""{self.persona_prompt}

    ## Current Time
    {current_datetime_str(self.timezone)}

    ## Your Current Task
    {self.task.description}

    ## Workspace Context
    {bootstrap if bootstrap else "(no workspace bootstrap files found)"}

    {skills_block if skills_block else ""}

    ## How You Work
    You are completing a task in the background. Use tools to do the actual work — don't just describe what you would do. Be thorough but efficient.

    When you are DONE, deliver your findings directly to the user in your own voice. This is what they will read. Be specific, include evidence (file paths, command outputs, key findings), and stay in character.

    Do NOT end on a tool call. Your final message IS the deliverable.

    ## Available Tools
    - read_file: Read file contents
    - write_file: Create or overwrite files
    - edit_file: Edit existing files
    - list_dir: List directory contents
    - exec: Run shell commands
    - web_search: Search the web
    - web_fetch: Fetch web page content

    ## Workspace
    {self.workspace}"""
    
    async def run(self) -> None:
        """
        Execute the task. Guaranteed to complete or fail.
        Always pushes to completion queue.
        """
        try:
            self.registry.mark_started(self.task.id)
            logger.info(f"Worker started: {self.task.label} [{self.task.id}]")
            
            result = await self._execute()
            
            self.registry.mark_completed(self.task.id, result)
            logger.info(f"Worker completed: {self.task.label} [{self.task.id}]")
            
        except asyncio.CancelledError:
            self.registry.mark_cancelled(self.task.id, "cancelled")
            logger.info(f"Worker cancelled: {self.task.label} [{self.task.id}]")
            raise
        except Exception as e:
            error = str(e)
            self.registry.mark_failed(self.task.id, error)
            logger.error(f"Worker failed: {self.task.label} [{self.task.id}] - {e}")
        
        finally:
            # ALWAYS push to completion queue - guaranteed delivery
            await self.completion_queue.put(self.task)
    
    async def _execute(self) -> str:
        """
        Execute the task loop.

        Step budget is set by complexity. When approaching the limit, the LLM
        is warned to wrap up. If it still exhausts steps, a forced summary
        turn (no tools) guarantees a result.
        """
        messages = [
            {"role": "system", "content": self._build_system_prompt()},
            {"role": "user", "content": self.task.description},
        ]

        max_iterations = self.task.max_iterations  # None = unlimited
        warned = False
        warn_at = 0
        if max_iterations:
            # Warn the LLM when ~80% of budget is used so it can wrap up naturally
            warn_at = max(3, int(max_iterations * 0.8))

        final_result: str | None = None
        last_content: str | None = None
        tools_used: list[str] = []
        tool_outputs: list[str] = []

        def _one_line(s: str, limit: int = 140) -> str:
            s = " ".join((s or "").split()).strip()
            if len(s) > limit:
                s = s[: limit - 1].rstrip() + "…"
            return s

        def _format_action(tool_name: str, args: dict[str, Any]) -> str:
            """Human-readable action detail for progress/status; avoid internal tool jargon."""
            name = (tool_name or "").strip()
            args = args or {}
            if name == "exec":
                cmd = _one_line(str(args.get("command", "")))
                return f"running `{cmd}`" if cmd else "running a shell command"
            if name == "read_file":
                p = _one_line(str(args.get("path", "")))
                return f"reading `{p}`" if p else "reading a file"
            if name == "list_dir":
                p = _one_line(str(args.get("path", "")))
                return f"checking `{p}`" if p else "checking a folder"
            if name == "write_file":
                p = _one_line(str(args.get("path", "")))
                return f"writing `{p}`" if p else "writing a file"
            if name == "edit_file":
                p = _one_line(str(args.get("path", "")))
                return f"editing `{p}`" if p else "editing a file"
            if name == "web_search":
                q = _one_line(str(args.get("query", "")))
                return f"searching for “{q}”" if q else "searching the web"
            if name == "web_fetch":
                u = _one_line(str(args.get("url", "")))
                return f"opening {u}" if u else "opening a web page"
            if name == "message":
                return "sending a message"
            return "making progress"

        iteration = 0
        while True:
            iteration += 1
            self.registry.update_progress(self.task.id, iteration=iteration)

            # Nudge the LLM to start wrapping up (only if a cap exists)
            if max_iterations and iteration >= warn_at and not warned:
                warned = True
                remaining = max_iterations - iteration
                messages.append({
                    "role": "user",
                    "content": (
                        f"You have {remaining} steps remaining. Start wrapping up — "
                        "finish any critical tool calls, then deliver your final "
                        "answer to the user in your next response."
                    ),
                })

            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model,
            )

            if response.content:
                last_content = response.content

            if response.has_tool_calls:
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

                for tool_call in response.tool_calls:
                    action_detail = _format_action(tool_call.name, tool_call.arguments)
                    self.registry.update_progress(
                        self.task.id,
                        current_action=action_detail,
                    )

                    logger.debug(f"Worker [{self.task.id}] executing: {tool_call.name}")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)

                    tools_used.append(tool_call.name)
                    preview = result[:200] if result else "(empty)"
                    tool_outputs.append(f"{tool_call.name}: {preview}")

                    self.registry.update_progress(
                        self.task.id,
                        current_action="",
                        action_completed=action_detail,
                    )

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": result,
                    })
            else:
                # No tool calls = LLM is done, this is the final answer
                final_result = response.content
                break

            # If a cap exists and we've hit it, break and force summary below.
            if max_iterations and iteration >= max_iterations:
                break

            # Compact message history periodically to prevent unbounded context growth on long tasks.
            #
            # IMPORTANT: tool-role messages must not be "orphaned" (a tool message without its
            # preceding assistant tool_calls message can break tool-calling schemas). When we
            # keep a tail, ensure it does not start mid tool-response block.
            if len(messages) > 140:
                system = messages[0]

                def _safe_tail_start(msgs: list[dict[str, Any]], desired_tail: int) -> int:
                    start = max(1, len(msgs) - desired_tail)
                    # Never start on a tool message; include the preceding assistant tool_calls.
                    while start > 1 and (msgs[start].get("role") == "tool"):
                        start -= 1
                    return start

                start = _safe_tail_start(messages, desired_tail=80)
                tail = messages[start:]

                unique_tools = list(dict.fromkeys(tools_used))
                tool_summary = ", ".join(unique_tools[-20:]) if unique_tools else "none"
                recent_outputs = "\n".join(tool_outputs[-12:]) if tool_outputs else "No output captured."
                recent_actions = "\n".join(f"- {a}" for a in (self.task.actions_completed[-12:] or []))
                if not recent_actions:
                    recent_actions = "- (none yet)"

                summary = (
                    "Earlier steps summary (compressed to save context):\n"
                    f"- iterations so far: {iteration}\n"
                    f"- recent actions:\n{recent_actions}\n"
                    f"- tools used recently: {tool_summary}\n"
                    f"- recent tool output previews:\n{recent_outputs}\n"
                )
                messages = [system, {"role": "assistant", "content": summary}] + tail

        # --- Budget exhausted: force a summary with tools disabled ---
        if final_result is None:
            messages.append({
                "role": "user",
                "content": (
                    "You've used all available steps. Deliver your findings "
                    "and answer NOW, in your own voice, in character. "
                    "Be specific — include what you found, what you checked, "
                    "and any evidence from the tools you used."
                ),
            })
            try:
                summary_response = await self.provider.chat(
                    messages=messages,
                    tools=None,
                    model=self.model,
                )
                if summary_response.content and summary_response.content.strip():
                    final_result = summary_response.content
            except Exception as e:
                logger.warning(f"Worker [{self.task.id}] summary turn failed: {e}")

        if final_result is None and last_content:
            final_result = last_content

        # Absolute fallback: build from tool execution log
        if final_result is None:
            unique_tools = list(dict.fromkeys(tools_used))
            tool_summary = ", ".join(unique_tools) if unique_tools else "none"
            recent_outputs = "\n".join(tool_outputs[-5:]) if tool_outputs else "No output captured."
            final_result = (
                f"Task ran for {iteration} steps. Tools used: {tool_summary}.\n\n"
                f"Last tool outputs:\n{recent_outputs}"
            )

        def _final_is_unusable(text: str) -> bool:
            t = " ".join((text or "").split()).strip()
            if not t:
                return True
            if looks_like_prompt_leak(t):
                return True
            lower = t.lower()
            # Short, non-informative "receipts" are not acceptable as task results.
            if len(t.split()) < 6:
                return True
            # Avoid meta/tooling chatter in the final user-facing message.
            if any(tok in lower for tok in ["tool call", "tool_calls", "system prompt", "developer message"]):
                return True
            return False

        async def _force_finalization() -> str | None:
            """
            If the model ends with meta-instructions (prompt leakage) or
            an unhelpfully short message, do a tools-disabled "finalization"
            turn that is explicitly user-facing.
            """
            recent_actions = "\n".join(f"- {a}" for a in (self.task.actions_completed[-12:] or []))
            if not recent_actions:
                recent_actions = "- (none yet)"
            recent_outputs = "\n".join(tool_outputs[-10:]) if tool_outputs else "(no captured output previews)"

            finalizer_system = (
                f"{self.persona_prompt}\n\n"
                "You are writing the final message that will be sent to the user.\n"
                "Stay in character. Be concrete and helpful."
            )
            finalizer_user = (
                "Write the final user-facing message only.\n"
                "Exclude any meta-instructions, role labels, or tool-policy text.\n\n"
                f"Task: {self.task.label}\n\n"
                f"Requested:\n{self.task.description}\n\n"
                f"Recent actions:\n{recent_actions}\n\n"
                f"Recent output previews:\n{recent_outputs}\n"
            )

            msg = [
                {"role": "system", "content": finalizer_system},
                {"role": "user", "content": finalizer_user},
            ]

            try:
                r = await self.provider.chat(messages=msg, tools=None, model=self.model, max_tokens=800)
                out = (r.content or "").strip()
                return out or None
            except Exception as e:
                logger.warning(f"Worker [{self.task.id}] finalization turn failed: {e}")
                return None

        # If the model leaked prompt/instructions (or produced a uselessly short
        # "receipt"), try to recover before marking the task completed.
        if _final_is_unusable(final_result):
            recovered: str | None = None
            for _ in range(3):
                recovered = await _force_finalization()
                if recovered and not _final_is_unusable(recovered):
                    final_result = recovered
                    break

        return final_result


class WorkerPool:
    """
    Manages concurrent worker execution.

    Spawns workers as async tasks, tracks them, and ensures
    completion notifications are delivered.
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        registry: TaskRegistry,
        persona_prompt: str,
        model: str | None = None,
        brave_api_key: str | None = None,
        max_concurrent: int = 5,
        timezone: str | None = None,
    ):
        self.provider = provider
        self.workspace = workspace
        self.registry = registry
        self.persona_prompt = persona_prompt
        self.model = model
        self.brave_api_key = brave_api_key
        self.max_concurrent = max_concurrent
        self.timezone = timezone

        self.completion_queue: asyncio.Queue[Task] = asyncio.Queue()
        self._running: dict[str, asyncio.Task] = {}

    def spawn(self, task: Task) -> None:
        """Spawn a worker for the task."""
        worker = Worker(
            task=task,
            provider=self.provider,
            workspace=self.workspace,
            registry=self.registry,
            completion_queue=self.completion_queue,
            persona_prompt=self.persona_prompt,
            model=self.model,
            brave_api_key=self.brave_api_key,
            timezone=self.timezone,
        )

        async_task = asyncio.create_task(worker.run())
        self._running[task.id] = async_task

        # Cleanup when done
        def _on_done(_: asyncio.Task) -> None:
            self._running.pop(task.id, None)

        async_task.add_done_callback(_on_done)
        logger.info(f"Spawned worker for task: {task.label} [{task.id}]")

    def cancel(self, task_id: str) -> bool:
        """Cancel a running task by task id."""
        t = self._running.get(task_id)
        if not t:
            return False
        t.cancel()
        return True

    async def run_inline(self, task: Task) -> Task:
        """
        Run a task to completion in the current event loop.

        This is used for "direct"/one-shot invocations where background tasks
        would be cancelled when the process exits (e.g., `kyber agent -m ...`).
        """
        # Use a throwaway queue; Worker.run() always puts the task on completion.
        q: asyncio.Queue[Task] = asyncio.Queue()
        worker = Worker(
            task=task,
            provider=self.provider,
            workspace=self.workspace,
            registry=self.registry,
            completion_queue=q,
            persona_prompt=self.persona_prompt,
            model=self.model,
            brave_api_key=self.brave_api_key,
            timezone=self.timezone,
        )
        await worker.run()
        return task

    @property
    def active_count(self) -> int:
        """Number of currently running workers."""
        return len(self._running)

    async def get_completion(self) -> Task:
        """Wait for and return the next completed task."""
        return await self.completion_queue.get()

    def get_completion_nowait(self) -> Task | None:
        """Get a completed task if available, else None."""
        try:
            return self.completion_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
