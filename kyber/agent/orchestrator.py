"""
Orchestrator: The new agent architecture with guaranteed execution.

Key principles:
1. LLM declares intent, system executes (no hallucination possible)
2. Omniscient context - LLM always sees current system state
3. Character voice - everything sounds like the bot
4. Guaranteed delivery - completions always reach the user

The user can always chat while tasks run in the background.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from kyber.bus.events import InboundMessage, OutboundMessage
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider
from kyber.session.manager import SessionManager

from kyber.agent.task_registry import TaskRegistry, Task, TaskStatus
from kyber.agent.worker import WorkerPool
from kyber.agent.voice import CharacterVoice
from kyber.agent.context import ContextBuilder
from kyber.agent.intent import (
    Intent, IntentAction, AgentResponse, 
    RESPOND_TOOL, parse_tool_call
)


# Action-claiming phrases that require intent validation
ACTION_PHRASES = [
    "started", "kicked off", "working on", "in progress",
    "spawned", "running", "executing", "on it",
    "i'll", "i will", "let me", "going to",
    "i'm going to", "im going to", "gonna",
    "i shall", "we shall", "allow me", "let us",
]


# Note: don't use \\b word boundaries here; ⚡/✅ aren't "word" chars.
_TASK_REF_TOKEN_RE = re.compile(r"(?i)^[⚡✅]?[0-9a-f]{8}$")
_TASK_REF_INLINE_RE = re.compile(r"(?i)[⚡✅][0-9a-f]{8}")
_TASK_REF_PARENS_RE = re.compile(r"(?i)\\s*\\(\\s*[⚡✅]?[0-9a-f]{8}\\s*\\)\\s*")

def _extract_ref(text: str) -> str | None:
    """Extract a task reference token (⚡deadbeef / ✅deadbeef / deadbeef) from free text."""
    # Keep it simple and robust: scan tokens split on whitespace/punctuation.
    # We accept the first plausible token.
    candidates = re.findall(r"(?i)[⚡✅]?[0-9a-f]{8}", text)
    return candidates[0] if candidates else None


def _looks_like_status_request(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s:
        return False
    # Keep this conservative; if it triggers, we will never spawn work.
    if s.startswith(("status", "progress", "update")):
        return True
    if " status" in s or "progress" in s or "update" in s:
        return True
    if s.startswith("ref:"):
        return True
    return False


def _strip_fabricated_refs(text: str) -> str:
    """
    Remove task-reference blocks that the model might hallucinate.

    We reserve refs for system-generated task tracking. If the model includes
    "Ref:" (or a bare ⚡token) without a real spawned task, users get a broken
    workflow: they ask for status and the registry can't find it.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Case 1: "Ref: ⚡deadbeef" (single line)
        if stripped.lower().startswith("ref:"):
            after = stripped[4:].strip()
            if after and _TASK_REF_TOKEN_RE.fullmatch(after):
                i += 1
                continue

            # Case 2: "Ref:" then blank lines then token on next line(s)
            if not after:
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and _TASK_REF_TOKEN_RE.fullmatch(lines[j].strip()):
                    i = j + 1
                    continue

        # Case 3: bare "⚡deadbeef" line (common hallucination)
        if _TASK_REF_TOKEN_RE.fullmatch(stripped) and ("⚡" in stripped or "✅" in stripped):
            i += 1
            continue

        out.append(line)
        i += 1

    # Trim excessive blank lines introduced by deletions.
    cleaned = "\n".join(out).strip()
    return cleaned


def _strip_task_refs_for_chat(text: str) -> str:
    """Remove real task reference tokens from user-visible chat output."""
    if not text:
        return ""
    lines = (text or "").splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        low = stripped.lower()

        # Drop explicit reference lines.
        if low.startswith("ref:"):
            after = stripped[4:].strip()
            if after and _TASK_REF_TOKEN_RE.fullmatch(after):
                continue
        if low.startswith("reference:"):
            after = stripped[len("reference:") :].strip()
            if after and _TASK_REF_TOKEN_RE.fullmatch(after):
                continue
        if low.startswith("completion:"):
            after = stripped[len("completion:") :].strip()
            if after and _TASK_REF_TOKEN_RE.fullmatch(after):
                continue

        # Drop bare token-only lines like "⚡deadbeef" / "✅deadbeef".
        if _TASK_REF_TOKEN_RE.fullmatch(stripped) and ("⚡" in stripped or "✅" in stripped):
            continue

        cleaned = _TASK_REF_PARENS_RE.sub(" ", line)
        cleaned = _TASK_REF_INLINE_RE.sub("", cleaned)
        out.append(cleaned.rstrip())

    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _prefix_meta(text: str) -> str:
    """Prefix system/meta updates so they visually differ from normal chat."""
    t = (text or "").lstrip()
    if not t:
        return "⚡️"
    if t.startswith("⚡"):
        return t
    return f"⚡️ {t}"

def _is_only_meta_prefix(text: str) -> bool:
    t = (text or "").strip()
    return t in {"⚡️", "⚡"}


class Orchestrator:
    """
    The main agent orchestrator.
    
    Handles:
    - Message processing with structured intent
    - Task spawning and tracking
    - Progress updates (batched every 30s)
    - Completion notifications (guaranteed, in character)
    - Concurrent chat while tasks run
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        persona_prompt: str,
        model: str | None = None,
        brave_api_key: str | None = None,
        background_progress_updates: bool = True,
        task_history_path: Path | None = None,
        task_provider: LLMProvider | None = None,
        task_model: str | None = None,
        timezone: str | None = None,
    ):
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.background_progress_updates = background_progress_updates
        self.timezone = timezone

        # Task provider defaults to the chat provider if not specified
        _task_provider = task_provider or provider
        _task_model = task_model or _task_provider.get_default_model()

        # Core components
        self.registry = TaskRegistry(history_path=task_history_path)
        self.sessions = SessionManager(workspace)
        self.voice = CharacterVoice(persona_prompt, provider, model=self.model)
        self.context = ContextBuilder(workspace, timezone=timezone)
        self.workers = WorkerPool(
            provider=_task_provider,
            workspace=workspace,
            registry=self.registry,
            persona_prompt=persona_prompt,
            model=_task_model,
            brave_api_key=brave_api_key,
            timezone=timezone,
        )

        self._running = False
        self._last_progress_update: dict[str, datetime] = {}
        self._last_user_content: str = ""
    
    async def run(self) -> None:
        """
        Main run loop. Processes messages and handles completions.
        """
        self._running = True
        logger.info("Orchestrator started")
        
        # Start background tasks.
        # Completion notifications are always delivered (otherwise tasks feel "silent").
        completion_task: asyncio.Task | None = None
        progress_task: asyncio.Task | None = None
        completion_task = asyncio.create_task(self._completion_loop())
        if self.background_progress_updates:
            progress_task = asyncio.create_task(self._progress_loop())
        
        try:
            while self._running:
                try:
                    msg = await asyncio.wait_for(
                        self.bus.consume_inbound(),
                        timeout=1.0,
                    )
                    # Handle each message in its own task (non-blocking)
                    asyncio.create_task(self._handle_message(msg))
                except asyncio.TimeoutError:
                    continue
        finally:
            if completion_task:
                completion_task.cancel()
            if progress_task:
                progress_task.cancel()
    
    def stop(self) -> None:
        """Stop the orchestrator."""
        self._running = False
        logger.info("Orchestrator stopping")
    
    async def _handle_message(self, msg: InboundMessage) -> None:
        """Handle a single inbound message."""
        try:
            response = await self._process_message(msg)
            if response:
                await self.bus.publish_outbound(response)
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Sorry, something went wrong: {str(e)}",
            ))
    
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a message using structured intent.

        Flow:
        1. Inject current system state into context
        2. LLM responds with message + intent
        3. Validate honesty (claims match intent)
        4. Execute intent (spawn task, check status, etc.)
        5. Return response with any injected references
        """
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}")

        session = self.sessions.get_or_create(msg.session_key)

        # Fast-path: status queries should never rely on the model to choose intent,
        # and they should NEVER spawn new work.
        if _looks_like_status_request(msg.content or ""):
            ref = _extract_ref(msg.content or "")
            status = self.registry.get_status_for_ref(ref)  # if ref=None => all tasks
            status_voiced = await self.voice.speak_status(status)
            status_voiced = _strip_fabricated_refs(status_voiced)
            status_voiced = _strip_task_refs_for_chat(status_voiced)
            status_voiced = _prefix_meta(status_voiced)

            session.add_message("user", msg.content)
            session.add_message("assistant", status_voiced)
            self.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=status_voiced,
            )

        # Build context with omniscient state
        system_state = self.registry.get_context_summary()
        messages = self._build_messages(session, msg.content, system_state)

        # Get structured response from LLM
        self._last_user_content = msg.content
        agent_response = await self._get_structured_response(messages)

        if not agent_response:
            # Don't save error fallbacks to session history — they pollute
            # future context and can confuse the LLM.
            logger.error(
                f"No usable response from LLM for message from "
                f"{msg.channel}:{msg.sender_id} | model={self.model}"
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Sorry, I'm having trouble responding right now. (model: {self.model})",
            )

        # Validate honesty
        agent_response = await self._validate_honesty(agent_response, messages)

        # Execute intent and get final message
        final_message = await self._execute_intent(
            agent_response,
            msg.channel,
            msg.chat_id,
        )

        # Save to session
        session.add_message("user", msg.content)
        session.add_message("assistant", final_message)
        self.sessions.save(session)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_message,
        )
    
    def _build_messages(
        self,
        session: Any,
        current_message: str,
        system_state: str,
    ) -> list[dict[str, Any]]:
        """Build messages with full context from ContextBuilder."""
        # Build the rich system prompt (identity, instructions, bootstrap
        # files incl. SOUL.md persona, memory, skills).
        base_prompt = self.context.build_system_prompt()

        # Append the live system state and respond-tool instructions
        # that are specific to the orchestrator architecture.
        system_prompt = (
            f"{base_prompt}\n\n---\n\n"
            f"## Current System State\n{system_state}\n\n"
            "## How to Respond\n"
            "You MUST use the 'respond' tool for every response. This tool lets you:\n"
            "- Send a natural message to the user\n"
            "- Optionally request an action (spawn_task, check_status, etc.)\n\n"
            "IMPORTANT: Your message should be natural and in-character. The system will\n"
            "handle adding task references — you don't need to make them up."
        )

        messages = [{"role": "system", "content": system_prompt}]

        # Add history
        for h in session.get_history(max_messages=10):
            messages.append(h)

        # Add current message
        messages.append({"role": "user", "content": current_message})

        return messages
    
    async def _get_structured_response(
        self,
        messages: list[dict[str, Any]],
    ) -> AgentResponse | None:
        """Get structured response from LLM using function calling."""
        try:
            response = await self.provider.chat(
                messages=messages,
                tools=[RESPOND_TOOL],
                model=self.model,
            )

            # Detect provider errors returned as content
            if response.finish_reason == "error":
                logger.error(f"LLM returned error: {response.content}")
                return None

            if response.has_tool_calls:
                for tc in response.tool_calls:
                    if tc.name == "respond":
                        return parse_tool_call(tc)
                # Tool calls present but none named "respond" — the model
                # tried to invoke worker tools directly (common with weaker
                # models).  Treat this as an implicit spawn_task intent so
                # the request isn't silently dropped.
                tool_names = [tc.name for tc in (response.tool_calls or [])]
                logger.warning(
                    f"LLM returned tool calls but none named 'respond': {tool_names} — "
                    f"auto-wrapping as spawn_task"
                )
                return AgentResponse(
                    message="On it — working on that now.",
                    intent=Intent(
                        action=IntentAction.SPAWN_TASK,
                        task_description=self._last_user_content or "Execute the requested task",
                        task_label="Requested task",
                        complexity="moderate",
                    ),
                )

            # Fallback: LLM didn't use the tool, treat as pure chat
            if response.content:
                return AgentResponse(
                    message=response.content,
                    intent=Intent(action=IntentAction.NONE),
                )

            logger.error(
                f"LLM returned empty response | "
                f"finish_reason={response.finish_reason} | "
                f"has_tool_calls={response.has_tool_calls} | "
                f"tool_calls={len(response.tool_calls or [])} | "
                f"content={response.content!r}"
            )
            return None

        except Exception as e:
            logger.error(f"Error getting structured response: {e}")
            return None
    
    async def _validate_honesty(
        self,
        response: AgentResponse,
        messages: list[dict[str, Any]],
    ) -> AgentResponse:
        """
        Validate that the LLM's claims match its declared intent.
        
        If the message claims an action but intent doesn't declare it,
        force a retry.
        """
        message_lower = response.message.lower()
        claims_action = any(phrase in message_lower for phrase in ACTION_PHRASES)

        # If the model emits a task "Ref:" without actually spawning, status checks
        # will fail immediately. Treat this as an honesty violation.
        claims_ref = "ref:" in message_lower or ("⚡" in response.message) or ("✅" in response.message)

        if response.intent.action == IntentAction.NONE and (claims_action or claims_ref):
            logger.warning(
                f"Honesty violation: claims action but intent is {response.intent.action}"
            )
            
            # Retry with correction
            messages.append({
                "role": "assistant",
                "content": response.message,
            })
            messages.append({
                "role": "user",
                "content": (
                    "You said you would do something, but you didn't set "
                    "intent.action to 'spawn_task'. If you're going to do work, "
                    "you MUST declare it in the intent. Please respond again "
                    "with the correct intent. Also: never include a task Ref "
                    "unless the system is actually spawning a task."
                ),
            })
            
            retry = await self._get_structured_response(messages)
            if retry:
                return retry
        
        return response
    
    async def _execute_intent(
        self,
        response: AgentResponse,
        channel: str,
        chat_id: str,
    ) -> str:
        """
        Execute the declared intent and return the final message.
        
        This is where the system takes over - the LLM declared what it wants,
        now we actually do it and inject proof (references).
        """
        message = response.message
        intent = response.intent

        # Never allow hallucinated refs to leak into user-visible output.
        # For spawn_task we will inject the true system-generated ref later.
        message = _strip_fabricated_refs(message)
        
        if intent.action == IntentAction.SPAWN_TASK:
            # Create and spawn task
            task = self.registry.create(
                description=intent.task_description or message,
                label=intent.task_label or "Task",
                origin_channel=channel,
                origin_chat_id=chat_id,
                complexity=intent.complexity,
            )

            # If the orchestrator is not running its background loops (common in
            # one-shot CLI usage), "spawning" a background task is a lie: the
            # event loop will exit and cancel the worker. In that case, execute
            # inline and return the completion payload directly.
            if not self._running:
                await self.workers.run_inline(task)
                if task.status == TaskStatus.COMPLETED:
                    result = task.result or "Task completed."
                    message = result
                else:
                    error = task.error or "Unknown error"
                    message = f"Failed: {task.label} — {error}"
                logger.info(f"Inline-completed task: {task.label} [{task.reference}]")
            else:
                self.workers.spawn(task)

                # Never show internal refs in chat.

                logger.info(f"Spawned task: {task.label} [{task.reference}]")
        
        elif intent.action == IntentAction.CHECK_STATUS:
            # Get status and inject into message
            status = self.registry.get_status_for_ref(intent.task_ref)
            status_voiced = await self.voice.speak_status(status)
            status_voiced = _strip_task_refs_for_chat(status_voiced)
            message = status_voiced
        
        elif intent.action == IntentAction.CANCEL_TASK:
            # TODO: Implement task cancellation
            message = f"{message}\n\n(Task cancellation not yet implemented)"
        
        # IntentAction.NONE = pure chat, no modification needed
        
        return message
    
    async def _completion_loop(self) -> None:
        """
        Background loop that delivers task completions to the user.

        The worker already speaks in the bot's voice — its result is the
        message. This loop just delivers it (and may append refs if configured).
        """
        # If voice generation fails, do not send canned output; retry later.
        pending: list[tuple[Task, int, datetime]] = []  # (task, attempts, retry_at)

        while self._running:
            try:
                task: Task | None = None
                now = datetime.now()

                # Prefer retrying pending completions that are ready.
                if pending:
                    pending.sort(key=lambda x: x[2])
                    if pending[0][2] <= now:
                        task, attempts, _ = pending.pop(0)
                    else:
                        # Wait up to 1s or until next retry time.
                        wait_s = min(1.0, max(0.0, (pending[0][2] - now).total_seconds()))
                        task = await asyncio.wait_for(self.workers.get_completion(), timeout=wait_s)
                        attempts = 0
                else:
                    task = await asyncio.wait_for(self.workers.get_completion(), timeout=1.0)
                    attempts = 0

                ref = task.completion_reference or "✅"

                try:
                    if task.status == TaskStatus.COMPLETED:
                        # Deliver the worker's own final answer verbatim.
                        # The worker already runs its own _final_is_unusable() check
                        # (which includes looks_like_prompt_leak). Re-checking here
                        # is redundant and actively harmful: legitimate task results
                        # with quoted strings, markdown, etc. get falsely flagged,
                        # causing the completion to be re-voiced (slow) or dropped.
                        base = (task.result or "").strip()
                        if base:
                            notification = base
                        else:
                            # Worker produced empty result — generate a short summary.
                            notification = await self.voice.speak(
                                content=f"Finished {task.label}.",
                                context="task completion (background ping). Be natural, 1 sentence.",
                                strict_llm=True,
                            )
                    else:
                        # For failures/cancellations, generate voice from a rich factual payload.
                        # This avoids the common "only the ✅ref" failure mode and gives the
                        # model more to say without inventing details.
                        status_payload = task.to_full_summary()

                        def _has_substance(text: str) -> bool:
                            t = (text or "").strip()
                            if not t:
                                return False
                            body = t.replace(ref, "").strip()
                            return bool(body)

                        must = None
                        notification = await self.voice.speak(
                            content=status_payload,
                            context=(
                                "background task completion (failed or cancelled). "
                                "Speak naturally in character. "
                                "Be honest about failure/cancellation. "
                                "Use only the facts provided. "
                                "Do NOT output only the receipt."
                            ),
                            must_include=must,
                            strict_llm=True,
                        )
                        if not _has_substance(notification):
                            # One retry with more explicit constraints.
                            notification = await self.voice.speak(
                                content=status_payload,
                                context=(
                                    "background task completion (failed or cancelled). "
                                    "Write 1-2 short sentences with substance (not only the receipt). "
                                    "Mention the task label and the status (failed/cancelled). "
                                    "Use only the facts provided."
                                ),
                                must_include=must,
                                strict_llm=True,
                            )
                except Exception as e:
                    # Requeue for retry. Exponential backoff up to 60s.
                    delay = min(60.0, 2.0 ** min(6, attempts))
                    retry_at = datetime.now() + timedelta(seconds=delay)
                    pending.append((task, attempts + 1, retry_at))
                    logger.error(
                        f"Voice failed for completion '{task.label}' "
                        f"(attempt {attempts + 1}), retrying in {delay:.1f}s: {e}"
                    )
                    continue

                notification = _prefix_meta(notification)
                notification = _strip_task_refs_for_chat(notification)
                if not notification.strip() or _is_only_meta_prefix(notification):
                    # Don't lose the completion — requeue for retry so voice
                    # gets another chance to produce usable output.
                    delay = min(60.0, 2.0 ** min(6, attempts))
                    retry_at = datetime.now() + timedelta(seconds=delay)
                    pending.append((task, attempts + 1, retry_at))
                    logger.error(
                        f"Completion notification became empty after stripping refs for '{task.label}' "
                        f"(attempt {attempts + 1}), retrying in {delay:.1f}s"
                    )
                    continue
                try:
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=task.origin_channel,
                        chat_id=task.origin_chat_id,
                        content=notification,
                        is_background=True,
                    ))
                except Exception as e:
                    # publish_outbound failed — requeue so we don't lose the completion.
                    delay = min(60.0, 2.0 ** min(6, attempts))
                    retry_at = datetime.now() + timedelta(seconds=delay)
                    pending.append((task, attempts + 1, retry_at))
                    logger.error(
                        f"Failed to publish completion for '{task.label}' "
                        f"(attempt {attempts + 1}), retrying in {delay:.1f}s: {e}"
                    )
                    continue

                logger.info(f"Sent completion notification for: {task.label}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                # CRITICAL: If we have a task that was popped from the queue but
                # couldn't be processed, requeue it so it's not lost forever.
                if task is not None:
                    delay = min(60.0, 2.0 ** min(6, attempts))
                    retry_at = datetime.now() + timedelta(seconds=delay)
                    pending.append((task, attempts + 1, retry_at))
                    logger.error(
                        f"Unexpected error in completion loop for '{task.label}' "
                        f"(attempt {attempts + 1}), requeueing in {delay:.1f}s: {e}"
                    )
                else:
                    logger.error(f"Error in completion loop: {e}")
    
    async def _progress_loop(self) -> None:
        """
        Background loop that sends batched progress updates.
        
        Every 30 seconds, if there are active tasks with meaningful progress,
        send an update to the user.
        """
        while self._running:
            try:
                # Not configurable: progress pings are every 30 seconds.
                await asyncio.sleep(30.0)
                
                active_tasks = self.registry.get_active_tasks()
                if not active_tasks:
                    continue
                
                # Group by origin (channel:chat_id)
                by_origin: dict[str, list[Task]] = {}
                for task in active_tasks:
                    key = f"{task.origin_channel}:{task.origin_chat_id}"
                    if key not in by_origin:
                        by_origin[key] = []
                    by_origin[key].append(task)
                
                # Send progress update to each origin
                for origin_key, tasks in by_origin.items():
                    # Dashboard-originated tasks don't need chat progress updates —
                    # the dashboard polls /tasks for live status already.
                    if origin_key == "dashboard:dashboard":
                        continue

                    # Check if we should send (avoid spamming)
                    last_update = self._last_progress_update.get(origin_key)
                    if last_update:
                        elapsed = (datetime.now() - last_update).total_seconds()
                        if elapsed < 30.0 * 0.9:
                            continue
                    
                    # Build summaries
                    summaries = []
                    for task in tasks:
                        if not task.progress_updates_enabled:
                            continue
                        if task.status == TaskStatus.RUNNING:
                            if task.current_action:
                                action = task.current_action
                            elif task.actions_completed:
                                action = f"just finished {task.actions_completed[-1]}"
                            else:
                                action = "getting started"
                            if task.max_iterations:
                                step = f"step {task.iteration}/{task.max_iterations}"
                            else:
                                step = f"step {task.iteration}"
                            summaries.append(
                                f"{task.label} — {action} — {step}"
                            )
                    
                    if not summaries:
                        continue
                    
                    # Generate in-character update.
                    # Wrap in a timeout so a slow/hanging LLM call can't block
                    # the entire progress loop and starve subsequent ticks.
                    try:
                        update = await asyncio.wait_for(
                            self.voice.speak_progress(summaries),
                            timeout=20.0,
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            f"Voice timed out for progress update to {origin_key} "
                            f"(summaries={summaries!r})"
                        )
                        continue
                    except Exception as e:
                        # Voice failed even after internal retries — log the full
                        # context so we can diagnose what the LLM actually returned.
                        logger.error(
                            f"Voice failed for progress update to {origin_key} "
                            f"(summaries={summaries!r}): {e}"
                        )
                        continue
                    update = _strip_task_refs_for_chat(update)
                    if not update.strip() or _is_only_meta_prefix(_prefix_meta(update)):
                        # Don't send empty updates; treat as a voice failure for this tick.
                        logger.error(f"Progress update became empty after stripping refs to {origin_key}")
                        continue
                    update = _prefix_meta(update)
                    
                    # Parse origin
                    parts = origin_key.split(":", 1)
                    channel = parts[0]
                    chat_id = parts[1] if len(parts) > 1 else "direct"
                    
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=channel,
                        chat_id=chat_id,
                        content=update,
                        is_background=True,
                    ))
                    
                    self._last_progress_update[origin_key] = datetime.now()
                    logger.debug(f"Sent progress update to {origin_key}")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in progress loop: {e}")
    
    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """Process a message directly (for CLI or cron usage).
        
        When called from cron with delivery enabled, pass the target
        channel/chat_id so spawned background workers route their
        completions to the correct destination instead of 'cli:direct'.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
        )
        
        response = await self._process_message(msg)
        return response.content if response else ""
