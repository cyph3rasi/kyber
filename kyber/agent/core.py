"""Direct tool-calling agent core.

Hermes-style agent loop: LLM calls tools directly in a loop until
it produces a final text response. No intent intermediary, no OpenHands
dependency — just straightforward tool calling.

    User message → LLM → tool calls → execute → LLM → ... → final response

Plugs into kyber's existing bus/channel architecture:
    Channel → Bus → AgentCore → Bus → Channel
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

from kyber.agent.context import ContextBuilder
from kyber.agent.display import (
    get_cute_tool_message,
    build_tool_preview,
)
from kyber.agent.task_registry import Task, TaskRegistry, TaskStatus
from kyber.agent.tools.registry import registry
from kyber.bus.events import InboundMessage, OutboundMessage
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from kyber.session.manager import SessionManager, Session

logger = logging.getLogger(__name__)

AGENT_MESSAGE_TIMEOUT_SECONDS = 600.0
AGENT_LOOP_TIMEOUT_SECONDS = 600.0
AGENT_SINGLE_LLM_TIMEOUT_SECONDS = 600.0
AGENT_SINGLE_TOOL_TIMEOUT_SECONDS = 600.0
SESSION_TOOL_PERSIST_MAX_EVENTS_PER_TURN = 20
SESSION_TOOL_PERSIST_MAX_CHARS_PER_EVENT = 2000
SESSION_TOOL_CONTEXT_MAX_EVENTS = 20
SESSION_TOOL_CONTEXT_MAX_TOTAL_CHARS = 24000


# ── Current-agent registry ────────────────────────────────────────────
# A weakref to the most recently-constructed AgentCore. Use
# :func:`get_current_agent` from anywhere inside the gateway process
# (slash commands, cron jobs, heartbeat tasks) to reach the live agent
# without having to plumb a reference through each channel/API layer.
# Weakref so we don't pin the instance in memory if it's deliberately
# torn down (e.g. tests that spin up multiple agents).

import weakref as _weakref

_CURRENT_AGENT_REF: "_weakref.ReferenceType[AgentCore] | None" = None


def _set_current_agent(agent: "AgentCore") -> None:
    global _CURRENT_AGENT_REF
    _CURRENT_AGENT_REF = _weakref.ref(agent)


def get_current_agent() -> "AgentCore | None":
    """Return the most recently-constructed AgentCore in this process.

    Returns ``None`` if nothing has instantiated one yet (e.g. we're in
    a ``kyber chat`` REPL that only talks to the gateway over HTTP) or
    the instance was garbage-collected.
    """
    ref = _CURRENT_AGENT_REF
    if ref is None:
        return None
    return ref()


class AgentCore:
    """Direct tool-calling agent.
    
    Consumes messages from the bus, runs a tool-calling loop with the LLM,
    and publishes responses back to the bus.
    
    This replaces the intent-based Orchestrator with a simpler, more direct
    architecture inspired by hermes-agent.
    
    Args:
        bus: MessageBus for inbound/outbound messages.
        provider: LLM provider for chat completions.
        workspace: Path to the workspace directory.
        model: Model identifier (uses provider default if not set).
        max_iterations: Maximum tool-calling loop iterations per message.
                        `<= 0` means unlimited (bounded by timeout guards).
        max_history: Maximum conversation messages to include as context.
        persona_prompt: Optional persona/personality system prompt override.
        timezone: Timezone for datetime display.
        progress_callback: Optional async callback(channel, chat_id, status_line, status_key)
                           for tool execution progress updates. status_line is
                           a formatted string like "┊ 🔍 search    query  1.2s".
                           status_key identifies which in-flight request the update belongs to.
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 0,
        max_history: int = 20,
        persona_prompt: str | None = None,
        timezone: str | None = None,
        task_history_path: Path | None = None,
        progress_callback: Callable[[str, str, str, str], Awaitable[None]] | None = None,
        *,
        tool_result_max_chars: int = 20_000,
        tool_result_keep_recent: int = 3,
        history_summary_trigger: int = 30,
        history_summary_keep_recent: int = 12,
        per_channel_tool_policy: dict[str, dict[str, list[str]]] | None = None,
    ):
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.max_history = max_history
        self.persona_prompt = persona_prompt
        self.progress_callback = progress_callback

        # Token-saving knobs — see kyber.config.schema.AgentDefaults.
        self.tool_result_max_chars = max(1024, int(tool_result_max_chars))
        self.tool_result_keep_recent = max(0, int(tool_result_keep_recent))
        self.history_summary_trigger = max(0, int(history_summary_trigger))
        self.history_summary_keep_recent = max(2, int(history_summary_keep_recent))
        self.per_channel_tool_policy = per_channel_tool_policy or {}
        
        # Core components
        self.context = ContextBuilder(workspace, timezone=timezone)
        self.sessions = SessionManager(workspace)
        self.registry = TaskRegistry(history_path=task_history_path)
        
        # Discover and register all tools
        registry.discover()
        self._configure_tool_callbacks()

        # Expose this instance as the process-level "current agent" so
        # slash commands (/usage, /cancel) and anything else that fires
        # outside the agent's direct call graph can find it without
        # needing a reference plumbed through each channel. Channels
        # still get their own ``agent`` attribute via
        # ChannelManager.attach_agent, but this is the belt-and-suspenders
        # fallback when that attachment missed — e.g. a channel wired up
        # after the manager ran.
        _set_current_agent(self)

        # State
        self._running = False
        self._active_tasks: dict[int, asyncio.Task] = {}
        self._background_tasks: dict[str, asyncio.Task] = {}
        self._running_tasks_by_task_id: dict[str, asyncio.Task] = {}
        self._file_locks: dict[str, asyncio.Lock] = {}
        # Per-session locks for short critical sections (history snapshot + commit)
        # while allowing concurrent long-running turns in the same chat.
        self._session_locks: dict[str, asyncio.Lock] = {}
    
    # ── Public API ──────────────────────────────────────────────────
    
    async def run(self) -> None:
        """Main message processing loop. Consumes from bus indefinitely."""
        self._running = True
        logger.info(f"AgentCore started (model={self.model}, tools={len(registry)})")
        
        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(), timeout=1.0
                )
                # Process messages concurrently across chats.
                task = asyncio.create_task(self._handle_message(msg))
                active_key = id(task)
                self._active_tasks[active_key] = task
                task.add_done_callback(
                    lambda t, key=active_key: self._active_tasks.pop(key, None)
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error in agent loop: {e}")
                await asyncio.sleep(1)
    
    def stop(self) -> None:
        """Signal the agent to stop processing."""
        self._running = False
        # Cancel any active tasks
        for task in self._active_tasks.values():
            task.cancel()
    
    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:default",
        channel: str = "cli",
        chat_id: str = "default",
        tracked_task_id: str | None = None,
    ) -> str:
        """Process a message directly (for CLI/cron, bypassing the bus).
        
        Args:
            content: Message text.
            session_key: Session identifier.
            channel: Channel name.
            chat_id: Chat identifier.
        
        Returns:
            Agent's text response.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
        )
        session_lock = self._get_session_lock(session_key)
        auto_finalize = tracked_task_id is None
        task_tracker: dict[str, str | None] = {"id": tracked_task_id}

        try:
            async with session_lock:
                response_text = await self._process_message(
                    msg,
                    session_key,
                    tracked_task_id=tracked_task_id,
                    task_tracker=task_tracker,
                    session_lock_held=True,
                )
            task_id = task_tracker.get("id")
            if auto_finalize and task_id:
                tracked = self.registry.get(task_id)
                if tracked and tracked.status not in (
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                ):
                    self.registry.mark_completed(task_id, response_text)
            return response_text
        except asyncio.CancelledError:
            task_id = task_tracker.get("id")
            if auto_finalize and task_id:
                tracked = self.registry.get(task_id)
                if tracked and tracked.status not in (
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                ):
                    # Reached here only when the explicit cancel path (user
                    # action or API) did NOT mark the task first — that
                    # means the CancelledError was raised from outside
                    # (service restart, process shutdown, parent cancel).
                    self.registry.mark_cancelled(
                        task_id, "Interrupted (service restart or shutdown)"
                    )
            raise
        except Exception as e:
            task_id = task_tracker.get("id")
            if auto_finalize and task_id:
                tracked = self.registry.get(task_id)
                if tracked and tracked.status not in (
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                ):
                    self.registry.mark_failed(task_id, str(e))
            raise
        finally:
            task_id = task_tracker.get("id")
            if task_id:
                self._running_tasks_by_task_id.pop(task_id, None)

    def _configure_tool_callbacks(self) -> None:
        """Attach internal callback handlers to tools that rely on the agent runtime."""
        msg_tool = registry.get("message")
        if msg_tool and hasattr(msg_tool, "set_send_callback"):
            msg_tool.set_send_callback(self._publish_tool_message)

    def reset_session(self, session_key: str) -> None:
        """Clear a session and bump its ``started_at`` boundary.

        Called by ``/new`` (``cmd_new``). Before this existed the command
        dispatcher silently did nothing on Discord because ``AgentCore``
        had no ``reset_session`` and no ``session_store`` — a channel's
        session_key is sticky per channel_id, so usage numbers for "This
        session" kept climbing across every claimed reset.
        """
        if not session_key:
            return
        try:
            self.sessions.clear(session_key)
        except Exception:
            logger.warning("reset_session failed for %s", session_key, exc_info=True)

    def _get_tools_for_channel(self, channel: str) -> list[dict[str, Any]]:
        """Return tool schemas for this channel, honoring per-channel policy.

        Most of the per-request token budget on short messages is the tool
        catalog. Channels that never need (say) ``exec`` can drop it here
        instead of re-uploading the schema every loop iteration.
        """
        all_defs = registry.get_definitions()
        policy = self.per_channel_tool_policy.get((channel or "").strip().lower())
        if not policy:
            return all_defs

        allow = policy.get("allow") or []
        deny = set(policy.get("deny") or [])
        allow_all = (not allow) or ("*" in allow)
        allowed_set = set(allow) if not allow_all else None

        filtered: list[dict[str, Any]] = []
        for schema in all_defs:
            name = schema.get("function", {}).get("name") or schema.get("name", "")
            if name in deny:
                continue
            if allowed_set is not None and name not in allowed_set:
                continue
            filtered.append(schema)
        return filtered

    def _compact_older_tool_results(self, messages: list[dict[str, Any]]) -> None:
        """Replace the body of old tool messages with a short summary in place.

        Run after each LLM iteration. The model already consumed those
        results; re-sending every byte on subsequent iterations is wasted
        context. We keep the ``tool_result_keep_recent`` most recent results
        verbatim and compress the rest.
        """
        if self.tool_result_keep_recent <= 0:
            return
        tool_indices = [
            i for i, m in enumerate(messages) if m.get("role") == "tool"
        ]
        if len(tool_indices) <= self.tool_result_keep_recent:
            return
        to_compact = tool_indices[: -self.tool_result_keep_recent]
        for i in to_compact:
            body = str(messages[i].get("content") or "")
            if len(body) <= 400:
                continue  # already short; leave alone
            messages[i]["content"] = _one_line(body, 400)

    async def _publish_tool_message(self, outbound: OutboundMessage) -> None:
        """Publish outbound tool messages back through the message bus."""
        await self.bus.publish_outbound(outbound)

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock

    def get_file_lock(self, path: str | Path) -> asyncio.Lock:
        """Get a per-file async lock for write/edit operations."""
        key = str(Path(path).expanduser().resolve(strict=False))
        lock = self._file_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._file_locks[key] = lock
        return lock
    
    # ── Internal ────────────────────────────────────────────────────
    
    async def _handle_message(self, msg: InboundMessage) -> None:
        """Handle an inbound message: process and publish response."""
        task_tracker: dict[str, str | None] = {"id": None}
        try:
            # Hard upper bound so a runaway tool loop can't keep users waiting forever.
            response_text = await asyncio.wait_for(
                self._process_message(
                    msg,
                    msg.session_key,
                    tracked_task_id=None,
                    task_tracker=task_tracker,
                ),
                timeout=AGENT_MESSAGE_TIMEOUT_SECONDS,
            )
            task_id = task_tracker.get("id")
            if task_id:
                tracked = self.registry.get(task_id)
                if tracked and tracked.status != TaskStatus.CANCELLED:
                    self.registry.mark_completed(task_id, response_text)
            
            # Publish response to bus
            response = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=response_text,
            )
            await self.bus.publish_outbound(response)
        except asyncio.CancelledError:
            task_id = task_tracker.get("id")
            if task_id:
                tracked = self.registry.get(task_id)
                if tracked and tracked.status != TaskStatus.CANCELLED:
                    # Reached here only when the explicit cancel path (user
                    # action or API) did NOT mark the task first — that
                    # means the CancelledError was raised from outside
                    # (service restart, process shutdown, parent cancel).
                    self.registry.mark_cancelled(
                        task_id, "Interrupted (service restart or shutdown)"
                    )
            raise
        except asyncio.TimeoutError:
            task_id = task_tracker.get("id")
            if task_id:
                tracked = self.registry.get(task_id)
                if tracked and tracked.status != TaskStatus.CANCELLED:
                    self.registry.mark_failed(
                        task_id,
                        "I took too long and hit a timeout while working on that.",
                    )
            timeout_msg = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    "I took too long and hit a timeout while working on that. "
                    "Please try again with a shorter request."
                ),
            )
            await self.bus.publish_outbound(timeout_msg)
        except Exception as e:
            task_id = task_tracker.get("id")
            if task_id:
                tracked = self.registry.get(task_id)
                if tracked and tracked.status != TaskStatus.CANCELLED:
                    self.registry.mark_failed(task_id, str(e))
            logger.exception(f"Error handling message from {msg.session_key}")
            error_msg = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Sorry, I hit an error: {str(e)}",
            )
            await self.bus.publish_outbound(error_msg)
        finally:
            task_id = task_tracker.get("id")
            if task_id:
                self._running_tasks_by_task_id.pop(task_id, None)
    
    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str,
        tracked_task_id: str | None = None,
        task_tracker: dict[str, str | None] | None = None,
        session_lock_held: bool = False,
    ) -> str:
        """Core message processing: build context, run tool loop, return response."""

        # Build system prompt (split for stable prefix caching)
        static_system, dynamic_system = self._build_system_prompts(msg)

        # Get tool definitions — filtered by per-channel policy if set.
        tool_defs = self._get_tools_for_channel(msg.channel)

        # Build messages from a short critical section on shared session history.
        if session_lock_held:
            session = self.sessions.get_or_create(session_key)
            session.add_message("user", msg.content)
            messages = self._build_messages(static_system, dynamic_system, session)
            run_session = session
        else:
            session_lock = self._get_session_lock(session_key)
            async with session_lock:
                session = self.sessions.get_or_create(session_key)
                session.add_message("user", msg.content)
                messages = self._build_messages(static_system, dynamic_system, session)
                self.sessions.save(session)
            # Per-turn scratch session: tool messages stay isolated while turn runs.
            run_session = Session(key=session_key)
        
        status_key = str(msg.metadata.get("message_id") or f"run-{time.time_ns()}")
        status_intro = await self._build_status_intro(msg.content)

        # Run the tool-calling loop
        response_text = await self._run_loop(
            messages=messages,
            tools=tool_defs,
            session=run_session,
            context_channel=msg.channel,
            context_chat_id=msg.chat_id,
            context_status_key=status_key,
            context_status_intro=status_intro,
            tracked_task_id=tracked_task_id,
            tracked_task_description=msg.content,
            tracked_task_label=self._build_task_label(msg.content),
            task_tracker=task_tracker,
        )

        if session_lock_held:
            # Direct/caller-managed flow is already serialized by caller lock.
            run_session.add_message("assistant", response_text)
            self.sessions.save(run_session)
        else:
            # Commit assistant output atomically to shared session history.
            session_lock = self._get_session_lock(session_key)
            async with session_lock:
                shared = self.sessions.get_or_create(session_key)
                self._merge_turn_tool_messages(shared, run_session)
                shared.add_message("assistant", response_text)
                self.sessions.save(shared)
        
        return response_text

    def _spawn_task(
        self,
        task: Task,
        conversation_context: str | None = None,
        *,
        session_key: str | None = None,
        project_key: str | None = None,
    ) -> None:
        """Run a task asynchronously from the legacy task API."""
        del conversation_context
        del project_key

        if not task:
            return

        run_key = session_key or f"task:{task.id}"

        async def _runner() -> None:
            self.registry.mark_started(task.id)
            try:
                result = await self.process_direct(
                    task.description,
                    session_key=run_key,
                    channel=task.origin_channel,
                    chat_id=task.origin_chat_id,
                    tracked_task_id=task.id,
                )
            except asyncio.CancelledError:
                tracked = self.registry.get(task.id)
                if tracked and tracked.status != TaskStatus.CANCELLED:
                    self.registry.mark_cancelled(
                        task.id, "Interrupted (service restart or shutdown)"
                    )
                raise
            except Exception as exc:  # pragma: no cover
                logger.exception("Background task failed: %s", task.id)
                self.registry.mark_failed(task.id, str(exc))
                await self.bus.publish_outbound(OutboundMessage(
                    channel=task.origin_channel,
                    chat_id=task.origin_chat_id,
                    content=f"{task.label} failed: {exc}",
                    is_background=True,
                ))
                return

            final_text = (result or "").strip() or "Done."
            if task.status != TaskStatus.CANCELLED:
                self.registry.mark_completed(task.id, final_text)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=task.origin_channel,
                    chat_id=task.origin_chat_id,
                    content=final_text,
                    is_background=True,
                ))

        bg_task = asyncio.create_task(_runner())

        def _done(_: asyncio.Task) -> None:
            self._background_tasks.pop(task.id, None)

        bg_task.add_done_callback(_done)
        self._background_tasks[task.id] = bg_task

    def _cancel_task(self, task_id: str) -> bool:
        """Cancel a background task by task id."""
        task = self.registry.get(task_id)
        if not task or task.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            return False

        running_task = self._background_tasks.get(task_id) or self._running_tasks_by_task_id.get(task_id)
        if not running_task:
            return False
        if running_task.done():
            return False

        running_task.cancel()
        self.registry.mark_cancelled(task_id, "Cancelled by user")
        return True
    
    async def _run_loop(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        session: Session,
        context_channel: str = "",
        context_chat_id: str = "",
        context_status_key: str = "",
        context_status_intro: str = "",
        tracked_task_id: str | None = None,
        tracked_task_description: str = "",
        tracked_task_label: str = "Task",
        task_tracker: dict[str, str | None] | None = None,
    ) -> str:
        """The core hermes-style tool-calling loop.
        
        Calls the LLM, executes any tool calls, feeds results back,
        and loops until the LLM produces a final text response.
        """
        iteration = 0
        loop_start_time = time.time()
        max_loop_seconds = AGENT_LOOP_TIMEOUT_SECONDS
        max_single_llm_seconds = AGENT_SINGLE_LLM_TIMEOUT_SECONDS
        max_single_tool_seconds = AGENT_SINGLE_TOOL_TIMEOUT_SECONDS

        status_started = False

        # Eagerly create the tracked task BEFORE the first LLM call so
        # every round-trip (including the first, and single-turn replies
        # that never produce tool calls) can record its token usage.
        # Previously the task was created mid-loop on the first tool-
        # calling response, so /usage reported 0 for quick chats.
        if not tracked_task_id:
            task = self.registry.create(
                description=tracked_task_description or "chat turn",
                label=tracked_task_label or "Chat",
                origin_channel=context_channel or "cli",
                origin_chat_id=context_chat_id or "direct",
            )
            tracked_task_id = task.id
            self.registry.mark_started(task.id)
            cur = asyncio.current_task()
            if cur is not None:
                self._running_tasks_by_task_id[task.id] = cur
            if task_tracker is not None:
                task_tracker["id"] = task.id

        try:
            while True:
                if self.max_iterations > 0 and iteration >= self.max_iterations:
                    break

                if (time.time() - loop_start_time) > max_loop_seconds:
                    logger.warning("Agent loop timeout after %.1fs", time.time() - loop_start_time)
                    return (
                        "I got stuck while working on that and stopped early. "
                        "Please try again with a more specific request."
                    )

                iteration += 1
                if tracked_task_id:
                    self.registry.update_progress(
                        tracked_task_id,
                        iteration=iteration,
                        current_action="thinking",
                    )
                
                # Call LLM
                try:
                    response = await asyncio.wait_for(
                        self.provider.chat(
                            messages=messages,
                            tools=tools if tools else None,
                            model=self.model,
                        ),
                        timeout=max_single_llm_seconds,
                    )
                except asyncio.TimeoutError:
                    logger.error("LLM call timed out (iteration %s)", iteration)
                    return "I hit a timeout while waiting on the model. Please try again."
                except Exception as e:
                    logger.error(f"LLM call failed (iteration {iteration}): {e}")
                    return f"I had trouble reaching the AI model: {str(e)}"

                # Accumulate token usage on the tracked task so /usage
                # and /cost can see real numbers. Providers that don't
                # return usage (some local shims) pass an empty dict and
                # add_usage no-ops.
                if tracked_task_id:
                    try:
                        self.registry.add_usage(
                            tracked_task_id,
                            getattr(response, "usage", None),
                            provider=type(self.provider).__name__,
                            model=self.model,
                        )
                    except Exception:  # pragma: no cover — accounting is best-effort
                        logger.debug("Couldn't record usage on task", exc_info=True)

                # If the LLM returned tool calls — execute them
                if response.has_tool_calls:
                    if self.progress_callback and not status_started:
                        try:
                            await self.progress_callback(
                                context_channel,
                                context_chat_id,
                                "__KYBER_STATUS_START__",
                                context_status_key,
                            )
                            await self.progress_callback(
                                context_channel,
                                context_chat_id,
                                context_status_intro or "✅ Task: In progress.",
                                context_status_key,
                            )
                            status_started = True
                        except Exception:
                            pass

                    if not tracked_task_id:
                        task = self.registry.create(
                            description=tracked_task_description,
                            label=tracked_task_label,
                            origin_channel=context_channel or "cli",
                            origin_chat_id=context_chat_id or "direct",
                        )
                        tracked_task_id = task.id
                        self.registry.mark_started(task.id)
                        cur = asyncio.current_task()
                        if cur is not None:
                            self._running_tasks_by_task_id[task.id] = cur
                        if task_tracker is not None:
                            task_tracker["id"] = task.id

                    # Add assistant message with tool calls to conversation
                    assistant_msg = self._build_tool_call_message(response)
                    messages.append(assistant_msg)

                    # Execute each tool call
                    for tc in response.tool_calls:
                        logger.info(f"Tool call: {tc.name}({_summarize_args(tc.arguments)})")
                        tool_preview = build_tool_preview(tc.name, tc.arguments)
                        action_line = f"{tc.name} {tool_preview}".strip() if tool_preview else tc.name
                        if tracked_task_id:
                            self.registry.update_progress(
                                tracked_task_id,
                                iteration=iteration,
                                current_action=action_line,
                            )

                        # Emit status before tool execution so users see what is
                        # starting, not only what has already finished.
                        if self.progress_callback:
                            try:
                                status_line = get_cute_tool_message(
                                    tc.name,
                                    tc.arguments,
                                    0.0,
                                )
                                await self.progress_callback(
                                    context_channel,
                                    context_chat_id,
                                    status_line,
                                    context_status_key,
                                )
                            except Exception:
                                pass

                        # Execute the tool
                        try:
                            result = await asyncio.wait_for(
                                self._execute_tool(
                                    tc,
                                    context_channel,
                                    context_chat_id,
                                    task_id=session.key,
                                ),
                                timeout=max_single_tool_seconds,
                            )
                        except asyncio.TimeoutError:
                            result = json.dumps(
                                {"error": f"Tool '{tc.name}' timed out after {int(max_single_tool_seconds)}s"}
                            )

                        # Add tool result to conversation
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        }
                        messages.append(tool_msg)

                        # Also record in session for persistence
                        session.add_message(
                            "tool",
                            self._trim_for_session_tool_context(result),
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                        )
                        if tracked_task_id:
                            self.registry.update_progress(
                                tracked_task_id,
                                action_completed=action_line,
                            )

                    # Compact older tool outputs before the next LLM call —
                    # the model already consumed them on this iteration.
                    self._compact_older_tool_results(messages)

                    # Loop back to call LLM again with tool results
                    continue
                
                # No tool calls — we have a final text response
                if response.content:
                    return response.content
                
                # Edge case: no content and no tool calls
                logger.warning(f"Empty response from LLM at iteration {iteration}")
                continue
            
            # Exceeded max iterations
            logger.warning(f"Agent loop hit max iterations ({self.max_iterations})")
            return "I ran out of steps trying to complete that. Could you try breaking it into smaller pieces?"
        finally:
            if status_started and self.progress_callback:
                try:
                    await self.progress_callback(
                        context_channel,
                        context_chat_id,
                        "__KYBER_STATUS_END__",
                        context_status_key,
                    )
                except Exception:
                    pass

    async def _build_status_intro(self, content: str, limit: int = 120) -> str:
        """Build a short status line that identifies the request being worked on."""
        text = " ".join((content or "").strip().split())
        if not text:
            return "✅ Task: In progress."

        if len(text) > limit:
            text = text[: max(0, limit - 3)].rstrip() + "..."
        return f"✅ Task: {text}"

    def _build_task_label(self, content: str, limit: int = 80) -> str:
        """Build a concise task label from user input for dashboard lists."""
        text = " ".join((content or "").strip().split())
        if not text:
            return "Task"
        if len(text) > limit:
            text = text[: max(0, limit - 3)].rstrip() + "..."
        return text
    
    async def _execute_tool(
        self,
        tc: ToolCallRequest,
        channel: str = "",
        chat_id: str = "",
        task_id: str = "",
    ) -> str:
        """Execute a single tool call and return the result string."""
        try:
            # Special handling for message tool — set context
            msg_tool = registry.get("message")
            if msg_tool and hasattr(msg_tool, "set_context"):
                msg_tool.set_context(channel, chat_id)
            
            result = await registry.execute(
                tc.name,
                tc.arguments,
                task_id=task_id,
                session_key=task_id,
                context_channel=channel,
                context_chat_id=chat_id,
                agent_core=self,
            )
            
            # Truncate very long results. The cap is configurable via
            # ``AgentDefaults.tool_result_max_chars`` and defaults to 20KB,
            # down from the old 100KB which was big enough to blow out a
            # context window on a single ``read_file`` of a minified JS
            # bundle.
            cap = self.tool_result_max_chars
            if len(result) > cap:
                result = result[:cap] + f"\n... (truncated, {len(result) - cap} more chars)"

            return result
            
        except Exception as e:
            logger.exception(f"Tool execution error: {tc.name}")
            return json.dumps({"error": str(e)})

    def _trim_for_session_tool_context(self, content: str, max_chars: int = SESSION_TOOL_PERSIST_MAX_CHARS_PER_EVENT) -> str:
        """Compress tool output before persisting it into session context."""
        text = " ".join((content or "").strip().split())
        if len(text) > max_chars:
            return text[: max(0, max_chars - 3)].rstrip() + "..."
        return text

    def _merge_turn_tool_messages(self, shared: Session, turn_session: Session) -> None:
        """Persist compact tool outputs from the current turn into shared session history."""
        tool_msgs = [
            m for m in turn_session.messages
            if (m.get("role") == "tool") and str(m.get("content") or "").strip()
        ]
        if not tool_msgs:
            return
        for m in tool_msgs[-SESSION_TOOL_PERSIST_MAX_EVENTS_PER_TURN:]:
            shared.add_message(
                "tool",
                self._trim_for_session_tool_context(str(m.get("content") or "")),
                tool_name=str(m.get("tool_name") or ""),
                tool_call_id=str(m.get("tool_call_id") or ""),
            )

    def _build_recent_tool_context_block(self, history: list[dict[str, Any]]) -> str | None:
        """Build a bounded tool-context block from recent session messages."""
        recent_tools: list[str] = []
        for msg in history:
            if msg.get("role") != "tool":
                continue
            content = self._trim_for_session_tool_context(str(msg.get("content") or ""))
            if not content:
                continue
            tool_name = str(msg.get("tool_name") or "tool").strip() or "tool"
            recent_tools.append(f"- [{tool_name}] {content}")

        if not recent_tools:
            return None

        kept: list[str] = []
        total = 0
        for line in reversed(recent_tools[-SESSION_TOOL_CONTEXT_MAX_EVENTS:]):
            line_len = len(line)
            if kept and (total + line_len) > SESSION_TOOL_CONTEXT_MAX_TOTAL_CHARS:
                break
            kept.append(line)
            total += line_len

        if not kept:
            return None

        kept.reverse()
        return (
            "Recent tool outputs from this chat. Reuse this context before re-running discovery commands:\n"
            + "\n".join(kept)
        )
    
    def _build_system_prompts(self, msg: InboundMessage) -> tuple[str, str]:
        """Return ``(static_prefix, dynamic_suffix)``.

        Prompt caching only works when the prefix is byte-identical call to
        call. Channel/chat/user fields obviously differ per chat, so we keep
        them in a separate second system message that comes AFTER the pinned
        prefix. On Anthropic endpoints the static half carries a
        ``cache_control: ephemeral`` marker via
        ``OpenAIProvider._prepare_messages_for_caching``.
        """
        base_prompt = self.context.build_system_prompt()

        static_parts: list[str] = []
        if self.persona_prompt:
            static_parts.append(self.persona_prompt)
        static_parts.append(base_prompt)

        dynamic_prompt = (
            "## Current Context\n"
            f"Channel: {msg.channel}\n"
            f"Chat ID: {msg.chat_id}\n"
            f"User: {msg.sender_id}"
        )

        return "\n\n".join(static_parts), dynamic_prompt

    def _build_messages(
        self,
        static_system: str,
        dynamic_system: str,
        session: Session,
    ) -> list[dict[str, Any]]:
        """Assemble the messages list for an LLM call.

        Layout (ordered so the cached prefix is as long as possible):

        1. Static system prompt — carries the ``_kyber_cache_pin`` flag that
           ``OpenAIProvider`` translates into an Anthropic cache breakpoint.
        2. Dynamic system prompt — channel/chat/user context.
        3. (Optional) rolled-up summary of turns older than
           ``history_summary_trigger``.
        4. (Optional) recent tool-output context block.
        5. The last ``max_history`` user/assistant turns verbatim.
        """
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": static_system,
                # Private flag picked up by OpenAIProvider and stripped
                # before the request leaves the machine.
                "_kyber_cache_pin": True,
            },
            {"role": "system", "content": dynamic_system},
        ]

        all_msgs = session.messages
        summary_block, recent_history = self._split_history_for_summary(all_msgs)
        if summary_block:
            messages.append({"role": "system", "content": summary_block})

        # Trim the recent window to max_history so it never grows unbounded
        # even with summarization disabled.
        history = (
            recent_history[-self.max_history:]
            if len(recent_history) > self.max_history
            else recent_history
        )

        tool_context = self._build_recent_tool_context_block(history)
        if tool_context:
            messages.append({"role": "system", "content": tool_context})

        for m in history:
            role = m.get("role")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": str(m.get("content") or "")})

        return messages

    def _split_history_for_summary(
        self,
        all_msgs: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Heuristic history rollup with zero extra API calls.

        When the session has more than ``history_summary_trigger`` messages,
        everything older than ``history_summary_keep_recent`` gets compressed
        into a small "Earlier in this conversation" block listing the user
        prompts (truncated) and a hint at each assistant reply. Deterministic
        output means the cached prefix stays stable across turns until new
        messages roll into the summary.
        """
        trigger = self.history_summary_trigger
        if trigger <= 0 or len(all_msgs) <= trigger:
            return None, all_msgs

        keep = self.history_summary_keep_recent
        older = all_msgs[:-keep] if keep > 0 else list(all_msgs)
        recent = all_msgs[-keep:] if keep > 0 else []
        if not older:
            return None, all_msgs

        lines: list[str] = []
        pending_user: str | None = None
        for m in older:
            role = m.get("role")
            content = str(m.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                pending_user = _one_line(content, 140)
            elif role == "assistant":
                assistant_hint = _one_line(content, 100)
                if pending_user:
                    lines.append(f"- user: {pending_user} → assistant: {assistant_hint}")
                    pending_user = None
                else:
                    lines.append(f"- assistant: {assistant_hint}")
        if pending_user:
            lines.append(f"- user: {pending_user} (no reply yet)")

        if not lines:
            return None, all_msgs

        # Cap the summary itself so a runaway session doesn't bloat the prefix.
        max_lines = 40
        if len(lines) > max_lines:
            dropped = len(lines) - max_lines
            lines = [f"- (…{dropped} earlier turns elided…)"] + lines[-max_lines:]

        block = (
            "## Earlier in this conversation\n"
            "Summary of turns that happened before the most recent "
            f"{len(recent)} messages. Use this for continuity; don't treat "
            "it as verbatim history.\n"
            + "\n".join(lines)
        )
        return block, recent
    
    def _build_tool_call_message(self, response: LLMResponse) -> dict[str, Any]:
        """Build an assistant message dict with tool calls (OpenAI format)."""
        tool_calls = []
        for tc in response.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            })
        
        msg: dict[str, Any] = {
            "role": "assistant",
            "tool_calls": tool_calls,
        }
        
        # Include content if present (some models return both)
        if response.content:
            msg["content"] = response.content
        else:
            msg["content"] = None
        
        return msg

def _one_line(text: str, limit: int = 120) -> str:
    """Collapse whitespace and truncate to ``limit`` chars."""
    flat = " ".join((text or "").strip().split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + "…"


def _summarize_args(args: dict[str, Any], max_len: int = 80) -> str:
    """Summarize tool arguments for logging."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")
    summary = ", ".join(parts)
    if len(summary) > max_len:
        summary = summary[:max_len - 3] + "..."
    return summary
