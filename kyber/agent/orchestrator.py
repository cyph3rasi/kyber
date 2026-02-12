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
import inspect
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from kyber.bus.events import InboundMessage, OutboundMessage
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider
from kyber.providers.history import dicts_to_model_messages
from kyber.session.manager import SessionManager

from kyber.agent.task_registry import TaskRegistry, Task, TaskStatus
from kyber.agent.worker import WorkerPool
from kyber.agent.voice import CharacterVoice
from kyber.agent.context import ContextBuilder
from kyber.agent.narrator import LiveNarrator
from kyber.agent.intent import (
    Intent, IntentAction, AgentResponse,
    RESPOND_TOOL, parse_tool_call, parse_response_content
)


@dataclass
class ChatDeps:
    """Dependencies passed to PydanticAI agent via RunContext.

    This is for future use — the orchestrator passes it to the provider,
    and eventually PydanticAI tools/instructions can access it via
    ``RunContext[ChatDeps]``.
    """
    system_state: str
    session_key: str | None
    persona_prompt: str
    timezone: str | None = None


# Action-claiming phrases that require intent validation
ACTION_PHRASES = [
    "started", "kicked off", "working on", "in progress",
    "spawned", "running", "executing", "on it",
    "i'll", "i will", "let me", "going to",
    "i'm going to", "im going to", "gonna",
    "i shall", "we shall", "allow me", "let us",
]


# Note: don't use \\b word boundaries here; ⚡/✅/❌ aren't "word" chars.
_TASK_REF_TOKEN_RE = re.compile(r"(?i)^[⚡✅❌]?[0-9a-f]{8}$")
_TASK_REF_INLINE_RE = re.compile(r"(?i)[⚡✅❌][0-9a-f]{8}")
_TASK_REF_PARENS_RE = re.compile(r"(?i)\\s*\\(\\s*[⚡✅❌]?[0-9a-f]{8}\\s*\\)\\s*")
_ABS_PATH_RE = re.compile(r"(?:(?:[A-Za-z]:\\\\|/)[^\s\"'`]+)")
_INLINE_CODE_RE = re.compile(r"`([^`]{1,300})`")
_FILENAME_HINT_RE = re.compile(
    r"\b[\w.-]+\.(?:py|js|ts|tsx|jsx|md|txt|json|yaml|yml|toml|html|css|sh|sql)\b",
    re.IGNORECASE,
)


def _is_truthy_env(value: str | None) -> bool:
    v = (value or "").strip().lower()
    return v in {"1", "true", "yes", "on"}

def _is_falsy_env(value: str | None) -> bool:
    v = (value or "").strip().lower()
    return v in {"0", "false", "no", "off"}


def _looks_like_follow_up_tweak(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if _extract_paths(raw) or _FILENAME_HINT_RE.search(raw):
        return False

    s = _normalize_for_match(raw)
    if not s:
        return False

    tweak_cues = (
        "tweak", "fix", "change", "update", "adjust", "still", "again",
        "capped", "limit", "too long", "too short", "character", "cap",
        "make it", "no longer", "keep", "reduce", "increase",
    )
    if any(c in s for c in tweak_cues):
        return True

    # Very short follow-up statements are often minor tweaks to recent work.
    return len(s.split()) <= 10
def _looks_like_deferred_execution_promise(text: str) -> bool:
    """
    Detect promises to do work later ("I'll spawn a task/sub-agent...")
    so we can force immediate follow-through.
    """
    raw = (text or "").strip().lower()
    if not raw:
        return False

    future_cues = ("i'll", "i will", "let me", "going to", "gonna")
    launch_cues = ("spawn", "spin up", "start", "kick off", "launch", "run")
    unit_cues = ("task", "sub-agent", "sub agent", "agent")
    return (
        any(c in raw for c in future_cues)
        and any(c in raw for c in launch_cues)
        and any(c in raw for c in unit_cues)
    )


def _is_security_scan_request(text: str) -> bool:
    """Detect if a task description is requesting a security scan."""
    s = (text or "").lower()
    return any(phrase in s for phrase in [
        "security scan",
        "security audit",
        "security check",
        "scan my system",
        "scan for vulnerabilities",
        "scan for threats",
        "scan for malware",
        "run a scan",
        "full scan",
        "environment scan",
    ])


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
        if _TASK_REF_TOKEN_RE.fullmatch(stripped) and ("⚡" in stripped or "✅" in stripped or "❌" in stripped):
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

        # Drop bare token-only lines like "⚡deadbeef" / "✅deadbeef" / "❌deadbeef".
        if _TASK_REF_TOKEN_RE.fullmatch(stripped) and ("⚡" in stripped or "✅" in stripped or "❌" in stripped):
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
        return "💎"
    if t.startswith(("⚡", "💎")):
        return t
    return f"💎 {t}"

def _is_only_meta_prefix(text: str) -> bool:
    t = (text or "").strip()
    return t in {"⚡️", "⚡", "💎"}

def _extract_paths(text: str) -> list[str]:
    """Extract absolute filesystem-like paths from free text."""
    if not text:
        return []
    out: list[str] = []
    for raw in _ABS_PATH_RE.findall(text):
        path = raw.rstrip(".,;:)]}\"'")
        if path.startswith(("http://", "https://")):
            continue
        if len(path) < 3:
            continue
        if path not in out:
            out.append(path)
    return out

def _normalize_for_match(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return " ".join(t.split())

def _is_affirmative_confirmation(text: str) -> bool:
    s = _normalize_for_match(text)
    if not s:
        return False
    exact = {
        "yes", "y", "ok", "okay", "proceed", "continue", "approved",
        "confirm", "do it", "go ahead", "sounds good", "ship it",
    }
    if s in exact:
        return True
    phrases = [
        "please proceed",
        "go ahead and",
        "yes proceed",
        "do it now",
        "you can proceed",
        "proceed with it",
    ]
    return any(p in s for p in phrases)

def _is_negative_confirmation(text: str) -> bool:
    s = _normalize_for_match(text)
    if not s:
        return False
    exact = {
        "no", "n", "stop", "cancel", "dont", "do not",
        "not now", "hold off", "skip",
    }
    if s in exact:
        return True
    phrases = [
        "do not proceed",
        "dont proceed",
        "stop that",
        "cancel that",
    ]
    return any(p in s for p in phrases)

def _looks_like_confirmation_prompt(text: str) -> bool:
    s = (text or "").lower()
    if not s:
        return False
    cues = [
        "awaiting your approval",
        "awaiting approval",
        "ready to proceed",
        "confirm delete",
        "confirm cleanup",
        "confirm before",
        "should i proceed",
        "do you want me to proceed",
        "ready to proceed with cleanup",
    ]
    if any(c in s for c in cues):
        return True
    return "?" in s and ("proceed" in s or "confirm" in s or "approval" in s)


class Orchestrator:
    """
    The main agent orchestrator.

    Handles:
    - Message processing with structured intent
    - Task spawning and tracking
    - Progress updates (batched every 60s)
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
        exec_timeout: int = 60,
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
        live_updates_env = os.getenv("KYBER_LIVE_UPDATES")
        # Default ON: users should see active progress, not a long typing indicator.
        self._live_updates_enabled = not _is_falsy_env(live_updates_env)
        self.narrator: LiveNarrator | None = None
        if self._live_updates_enabled:
            chunk_seconds = 45.0
            raw_chunk = (os.getenv("KYBER_LIVE_UPDATE_CHUNK_SECONDS", "") or "").strip()
            if raw_chunk:
                try:
                    chunk_seconds = max(10.0, float(raw_chunk))
                except ValueError:
                    chunk_seconds = 45.0
            # Optional live action streaming. Disabled by default for cleaner UX.
            self.narrator = LiveNarrator(
                flush_callback=self._send_narration,
                flush_interval=chunk_seconds,
            )

        self.workers = WorkerPool(
            provider=_task_provider,
            workspace=workspace,
            registry=self.registry,
            persona_prompt=persona_prompt,
            model=_task_model,
            brave_api_key=brave_api_key,
            timezone=timezone,
            exec_timeout=exec_timeout,
            narrator=self.narrator,
        )

        self._running = False
        self._last_progress_update: dict[str, datetime] = {}
        self._last_progress_summary: dict[str, list[str]] = {}
        self._last_user_content: str = ""

    def _use_provider_native_orchestration(self) -> bool:
        """True when the provider should act as the full orchestrator brain."""
        probe = getattr(self.provider, "uses_provider_native_orchestration", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:
                return False
        return False

    @staticmethod
    def _session_key(channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"

    @staticmethod
    def _extract_inline_code_spans(text: str, limit: int = 12) -> list[str]:
        spans: list[str] = []
        for m in _INLINE_CODE_RE.findall(text or ""):
            s = m.strip()
            if not s:
                continue
            token = f"`{s}`"
            if token in spans:
                continue
            spans.append(token)
            if len(spans) >= limit:
                break
        return spans

    def _remember_paths_in_session(self, session: Any, text: str) -> None:
        """Update session metadata with recently referenced absolute paths."""
        paths = _extract_paths(text)
        if not paths:
            return

        existing_raw = session.metadata.get("recent_paths")
        existing = existing_raw if isinstance(existing_raw, list) else []
        normalized = [str(p) for p in existing if isinstance(p, str)]

        for p in paths:
            if p in normalized:
                normalized.remove(p)
            normalized.append(p)

        session.metadata["recent_paths"] = normalized[-20:]

    def _augment_task_description_with_recent_paths(self, description: str, session: Any | None) -> str:
        """Inject recent path context so follow-up tasks can skip file rediscovery."""
        if session is None:
            return description
        paths_raw = session.metadata.get("recent_paths")
        if not isinstance(paths_raw, list):
            return description

        paths = [str(p) for p in paths_raw if isinstance(p, str)]
        if not paths:
            return description

        existing_text = (description or "")
        if any(p in existing_text for p in paths):
            return description

        top_paths = paths[-4:]
        follow_up_mode = _looks_like_follow_up_tweak(existing_text)

        if follow_up_mode:
            context_block = (
                "\n\nFollow-up tweak mode (important):\n"
                "This appears to be a tweak to very recent work.\n"
                "Start by opening these likely target files directly:\n"
                + "\n".join(f"- {p}" for p in top_paths)
                + "\n\nExecution rules:\n"
                "- Do NOT begin with broad workspace discovery commands.\n"
                "- Avoid workspace-wide scans (`ls -la <workspace>`, `find <workspace>`, or broad recursive `rg`) "
                  "unless none of the files above are relevant.\n"
                "- If one of the files above is relevant, edit it immediately."
            )
        else:
            context_block = (
                "\n\nConversation context (recent file paths):\n"
                + "\n".join(f"- {p}" for p in top_paths)
                + "\n\nIf this is a tweak/fix to recent work, start with those files before broad filesystem discovery."
            )

        return (description or "") + context_block

    def _build_pending_confirmation(self, task: Task, notification: str) -> dict[str, Any] | None:
        """Return pending-confirmation metadata if a completion asks for approval."""
        if not _looks_like_confirmation_prompt(notification):
            return None
        return {
            "created_at": datetime.now().isoformat(),
            "task_id": task.id,
            "task_label": task.label,
            "plan": notification[:12000],
        }

    def _persist_completion_context(self, task: Task, notification: str) -> None:
        """Persist completion output into session history for follow-up turns."""
        key = self._session_key(task.origin_channel, task.origin_chat_id)
        session = self.sessions.get_or_create(key)
        session.add_message(
            "assistant",
            notification,
            is_background=True,
            task_id=task.id,
            task_status=task.status.value,
        )
        self._remember_paths_in_session(session, notification)

        pending = self._build_pending_confirmation(task, notification)
        if pending:
            session.metadata["pending_confirmation"] = pending

        self.sessions.save(session)

    async def _maybe_handle_pending_confirmation(
        self,
        msg: InboundMessage,
        session: Any,
    ) -> OutboundMessage | None:
        """Handle terse approval/cancellation replies against pending plans."""
        pending = session.metadata.get("pending_confirmation")
        if not isinstance(pending, dict):
            return None

        raw = (msg.content or "").strip()
        if not raw:
            return None

        created_at_raw = pending.get("created_at")
        try:
            created_at = datetime.fromisoformat(str(created_at_raw))
        except Exception:
            created_at = None
        if created_at and (datetime.now() - created_at) > timedelta(hours=6):
            session.metadata.pop("pending_confirmation", None)
            self.sessions.save(session)
            return None

        if _is_negative_confirmation(raw):
            session.metadata.pop("pending_confirmation", None)
            reply = "Understood — I won’t execute that proposed plan."
            session.add_message("user", msg.content)
            session.add_message("assistant", reply)
            self._remember_paths_in_session(session, msg.content or "")
            self._remember_paths_in_session(session, reply)
            self.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=reply)

        if not _is_affirmative_confirmation(raw):
            return None

        plan = str(pending.get("plan") or "")
        task_label = str(pending.get("task_label") or "Approved follow-up")
        approved_description = (
            "The user approved the previously proposed plan. Execute it now.\n\n"
            "Approved plan context from prior task output:\n"
            f"{plan}\n\n"
            f"User confirmation: {raw}\n\n"
            "Carry out the approved changes directly. Do not restart with another broad audit "
            "unless absolutely required for safety."
        )

        response = AgentResponse(
            message="Proceeding with the approved plan now. I’ll report back when it’s done.",
            action=IntentAction.SPAWN_TASK,
            task_description=approved_description,
            task_label=task_label,
            complexity="moderate",
        )
        final_message = await self._execute_intent(response, msg.channel, msg.chat_id, session=session)

        session.metadata.pop("pending_confirmation", None)
        session.add_message("user", msg.content)
        session.add_message("assistant", final_message)
        self._remember_paths_in_session(session, msg.content or "")
        self._remember_paths_in_session(session, final_message)
        self.sessions.save(session)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_message,
        )

    async def run(self) -> None:
        """
        Main run loop. Processes messages and handles completions.
        """
        self._running = True
        logger.info("Orchestrator started")

        # Start the live narrator (replaces voice-based progress for active tasks)
        if self.narrator:
            self.narrator.start()

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
            if self.narrator:
                self.narrator.stop()
            if completion_task:
                completion_task.cancel()
            if progress_task:
                progress_task.cancel()

    def stop(self) -> None:
        """Stop the orchestrator."""
        self._running = False
        if self.narrator:
            self.narrator.stop()
        logger.info("Orchestrator stopping")

    async def _send_narration(self, channel: str, chat_id: str, message: str) -> None:
        """Callback for the LiveNarrator to send updates via the message bus."""
        content = message
        try:
            must_include = self._extract_inline_code_spans(message)
            timeout_s = 8.0
            raw_timeout = (os.getenv("KYBER_NARRATION_VOICE_TIMEOUT_SECONDS", "") or "").strip()
            if raw_timeout:
                try:
                    timeout_s = max(1.0, float(raw_timeout))
                except ValueError:
                    timeout_s = 8.0
            voiced = await asyncio.wait_for(
                self.voice.speak(
                    content=message,
                    context=(
                        "live task progress update in-character. Keep wording simple for non-technical users. "
                        "If commands/files are shown in inline code, preserve them exactly."
                    ),
                    must_include=must_include or None,
                    use_llm=True,
                    strict_llm=True,
                ),
                timeout=timeout_s,
            )
            if voiced and voiced.strip():
                content = voiced.strip()
        except Exception as e:
            logger.debug(f"Falling back to raw narration message: {e}")

        await self.bus.publish_outbound(OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=_prefix_meta(content),
            is_background=True,
        ))

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

        # Fast-path for approval/cancel follow-ups tied to prior worker output.
        # This avoids dropping context on terse messages like "proceed".
        pending_confirmation_out = await self._maybe_handle_pending_confirmation(msg, session)
        if pending_confirmation_out:
            return pending_confirmation_out

        if self._use_provider_native_orchestration():
            final_message = await self._process_with_provider_agent(msg, session)
            if not final_message:
                logger.error(
                    f"No usable response from provider-agent for message from "
                    f"{msg.channel}:{msg.sender_id} | model={self.model}"
                )
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Sorry, I'm having trouble responding right now. (model: {self.model})",
                )

            session.add_message("user", msg.content)
            session.add_message("assistant", final_message)
            self._remember_paths_in_session(session, msg.content or "")
            self._remember_paths_in_session(session, final_message)
            self.sessions.save(session)

            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=final_message,
            )

        # Build context with omniscient state
        system_state = self.registry.get_context_summary()
        self._last_user_content = msg.content

        # Always build messages (needed for honesty validation and legacy fallback)
        messages = self._build_messages(session, msg.content, system_state)

        # --- PydanticAI native structured output path ---
        # When the provider supports run_structured(), use it for direct
        # AgentResponse output (no tool-call parsing needed).
        agent_response = None
        if hasattr(self.provider, "run_structured"):
            agent_response = await self._get_structured_response_native(
                session, msg.content, system_state,
            )
            if not agent_response:
                user_msg_preview = (msg.content or "")[:100]
                logger.warning(
                    f"Native structured output failed, falling back to legacy path | "
                    f"model={self.model} | "
                    f"sender={msg.sender_id} | "
                    f"user_message_preview='{user_msg_preview}'"
                )

        if not agent_response:
            # Legacy path — used by non-PydanticAI providers or as fallback
            agent_response = await self._get_structured_response(messages, session_key=msg.session_key)

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

        # Validate honesty (retry uses legacy _get_structured_response path)
        agent_response = await self._validate_honesty(agent_response, messages, session_key=msg.session_key)

        # Execute intent and get final message
        final_message = await self._execute_intent(
            agent_response,
            msg.channel,
            msg.chat_id,
            session=session,
        )

        # Save to session
        session.add_message("user", msg.content)
        session.add_message("assistant", final_message)
        self._remember_paths_in_session(session, msg.content or "")
        self._remember_paths_in_session(session, final_message)
        self.sessions.save(session)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_message,
        )

    def _build_provider_agent_messages(
        self,
        session: Any,
        current_message: str,
        system_state: str,
    ) -> list[dict[str, Any]]:
        """Build conversation context for provider-native orchestration."""
        base_prompt = self.context.build_system_prompt()
        system_prompt = (
            f"{base_prompt}\n\n---\n\n"
            f"## Current System State\n{system_state}\n\n"
            "You are the sole orchestrator and executor for this conversation.\n"
            "Use available tools directly to do work when needed.\n"
            "Do not ask for permission; execute and report concrete outcomes.\n"
            "Never promise future work like 'I'll spawn a task' unless you execute it in this same turn.\n"
            "Never invent task references."
        )

        messages = [{"role": "system", "content": system_prompt}]
        use_native_ctx = bool(
            hasattr(self.provider, "uses_native_session_context")
            and callable(getattr(self.provider, "uses_native_session_context"))
            and self.provider.uses_native_session_context()
        )
        if not use_native_ctx:
            for h in session.get_history(max_messages=16):
                messages.append(h)
        messages.append({"role": "user", "content": current_message})
        return messages

    async def _process_with_provider_agent(self, msg: InboundMessage, session: Any) -> str | None:
        """Run a chat turn directly through the provider-native agent."""
        system_state = self.registry.get_context_summary()
        messages = self._build_provider_agent_messages(session, msg.content, system_state)
        self._last_user_content = msg.content
        try:
            response = await self._provider_chat(
                messages=messages,
                tools=None,
                model=self.model,
                session_key=msg.session_key,
            )
        except Exception as e:
            logger.error(f"Provider-agent chat failure: {e}")
            return None

        if response.finish_reason == "error":
            logger.error(f"Provider-agent returned error: {response.content}")
            return None

        final_message = (response.content or "").strip()
        parsed = parse_response_content(final_message)
        if parsed is not None and (parsed.message or "").strip():
            final_message = parsed.message.strip()
        if not final_message:
            logger.error(
                "Provider-agent returned empty response | "
                f"finish_reason={response.finish_reason} | "
                f"has_tool_calls={response.has_tool_calls}"
            )
            return None

        final_message = _strip_fabricated_refs(final_message)
        final_message = _strip_task_refs_for_chat(final_message)
        if _looks_like_deferred_execution_promise(final_message):
            logger.warning(
                "Provider-agent promised deferred execution; forcing immediate follow-through task"
            )
            forced = AgentResponse(
                message="On it.",
                action=IntentAction.SPAWN_TASK,
                task_description=(
                    "Follow through on this commitment and complete the work now.\n\n"
                    f"User message:\n{msg.content or ''}\n\n"
                    f"Assistant promise:\n{final_message}\n\n"
                    "Execute the promised changes immediately and report concrete outcomes."
                ),
                task_label="Follow-through task",
                complexity="moderate",
            )
            return await self._execute_intent(
                forced,
                msg.channel,
                msg.chat_id,
                session=session,
            )
        return final_message

    def _build_messages(
        self,
        session: Any,
        current_message: str,
        system_state: str,
    ) -> list[dict[str, Any]]:
        """Build messages with full context from ContextBuilder.

        Legacy path: used by non-PydanticAI providers and as a fallback for
        honesty-validation retries. When the provider supports
        ``run_structured()``, the primary flow uses
        ``_get_structured_response_native()`` instead.
        """
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

        use_native_ctx = bool(
            hasattr(self.provider, "uses_native_session_context")
            and callable(getattr(self.provider, "uses_native_session_context"))
            and self.provider.uses_native_session_context()
        )
        if not use_native_ctx:
            for h in session.get_history(max_messages=10):
                messages.append(h)

        # Add current message
        messages.append({"role": "user", "content": current_message})

        return messages

    async def _provider_chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        session_key: str | None,
    ) -> Any:
        supports_session_id = False
        supports_callback = False
        try:
            sig = inspect.signature(self.provider.chat)
            supports_session_id = "session_id" in sig.parameters
            supports_callback = "callback" in sig.parameters
        except Exception:
            supports_session_id = False
            supports_callback = False

        kwargs = {
            "messages": messages,
            "tools": tools,
            "model": model,
        }
        if supports_session_id:
            kwargs["session_id"] = session_key

        if supports_callback and not tools:
            # Create a callback that publishes updates to the bus
            # extracting channel/chat_id from session_key if possible
            channel, chat_id = "dashboard", "dashboard"
            if session_key and ":" in session_key:
                parts = session_key.split(":", 1)
                channel, chat_id = parts[0], parts[1]

            async def _progress_cb(msg: str) -> None:
                text = (msg or "").strip()
                # Hide internal orchestration/voice function-tool calls from users.
                if re.search(r"(?i)\busing tool:\s*`?(respond|say)`?\b", text):
                    return
                await self.bus.publish_outbound(OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=f"💎 {text}",
                    is_background=True,
                ))

            kwargs["callback"] = _progress_cb

        return await self.provider.chat(**kwargs)

    async def _get_structured_response(
        self,
        messages: list[dict[str, Any]],
        session_key: str | None = None,
    ) -> AgentResponse | None:
        """Get structured response from LLM using function calling.

        Legacy path: used by non-PydanticAI providers that don't support
        ``run_structured()``. When the provider supports it, the primary flow
        uses ``_get_structured_response_native()`` instead.
        """
        try:
            response = await self._provider_chat(
                messages=messages,
                tools=[RESPOND_TOOL],
                model=self.model,
                session_key=session_key,
            )

            # Detect provider errors returned as content
            if response.finish_reason == "error":
                logger.error(f"LLM returned error: {response.content}")
                return None

            if response.has_tool_calls:
                for tc in response.tool_calls:
                    if tc.name == "respond":
                        return parse_tool_call(tc)
                tool_names = [tc.name for tc in (response.tool_calls or [])]
                logger.warning(
                    f"LLM returned tool calls but none named 'respond': {tool_names}"
                )
                return None

            # Fallback: LLM didn't use the tool, treat as pure chat
            if response.content:
                parsed = parse_response_content(response.content)
                if parsed is not None:
                    return parsed
                return AgentResponse(
                    message=response.content,
                    action=IntentAction.NONE,
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

    async def _get_structured_response_native(
        self,
        session: Any,
        current_message: str,
        system_state: str,
    ) -> AgentResponse | None:
        """Get structured response using PydanticAI native structured output.

        This is the preferred path when the provider supports ``run_structured()``.
        It bypasses the legacy ``_build_messages()`` + ``_get_structured_response()``
        flow and instead:
        1. Builds an instructions string (system prompt + system state)
        2. Converts session history to PydanticAI ``ModelMessage`` format
        3. Calls ``provider.run_structured()`` to get ``AgentResponse`` directly

        Returns:
            AgentResponse on success, None on failure (same pattern as
            ``_get_structured_response``).
        """
        try:
            base_prompt = self.context.build_system_prompt()
            instructions = (
                f"{base_prompt}\n\n---\n\n"
                f"## Current System State\n{system_state}\n\n"
                "## How to Respond\n"
                "Respond with a natural message and an intent that describes any action to take.\n"
                "Your message should be natural and in-character. The system will\n"
                "handle adding task references — you don't need to make them up."
            )

            # Convert session history to PydanticAI ModelMessage format
            history_dicts = session.get_history(max_messages=10)
            message_history = dicts_to_model_messages(history_dicts)

            agent_response = await self.provider.run_structured(
                instructions=instructions,
                user_message=current_message,
                message_history=message_history or None,
                model=self.model,
            )
            return agent_response

        except Exception as e:
            # Enhanced logging for validation failures
            error_msg = str(e)
            user_msg_preview = (current_message or "")[:100]
            logger.error(
                f"Error getting native structured response: {error_msg} | "
                f"model={self.model} | "
                f"user_message_preview='{user_msg_preview}'"
            )
            # Log full details at debug level
            logger.debug(
                f"Native structured response failure details:\n"
                f"  Model: {self.model}\n"
                f"  Error: {error_msg}\n"
                f"  User message: {current_message}\n"
                f"  Exception type: {type(e).__name__}"
            )
            return None


    async def _validate_honesty(
        self,
        response: AgentResponse,
        messages: list[dict[str, Any]],
        session_key: str | None = None,
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

            retry = await self._get_structured_response(messages, session_key=session_key)
            if retry:
                return retry

        return response

    async def _execute_intent(
        self,
        response: AgentResponse,
        channel: str,
        chat_id: str,
        session: Any | None = None,
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
            desc = (intent.task_description or "").lower()
            label = (intent.task_label or "").lower()
            combined = f"{desc} {label}"

            task_description = self._augment_task_description_with_recent_paths(
                intent.task_description or message,
                session,
            )

            # Intercept security scan requests — route through the deterministic
            # scan path instead of letting the LLM improvise with the SKILL.md.
            if _is_security_scan_request(combined):
                task = self._spawn_security_scan(channel, chat_id)
                if task:
                    logger.info(f"Intercepted security scan → deterministic spawn [{task.reference}]")
                    return message
                # Fall through to normal spawn if the deterministic path fails

            # Create and spawn task
            task = self.registry.create(
                description=task_description,
                label=intent.task_label or "Task",
                origin_channel=channel,
                origin_chat_id=chat_id,
                complexity=intent.complexity,
            )

            # UX default: run inline so the experience feels like a normal
            # foreground coding session (immediate result, no async refs).
            # Keep dashboard-triggered tasks in background mode.
            run_inline = channel != "dashboard"

            if run_inline:
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
                logger.info(f"Spawned task: {task.label} [{task.reference}]")

        elif intent.action == IntentAction.CHECK_STATUS:
            status = self.registry.get_status_for_ref(intent.task_ref)
            status_voiced = await self.voice.speak_status(status)
            status_voiced = _strip_task_refs_for_chat(status_voiced)
            message = status_voiced

        elif intent.action == IntentAction.CANCEL_TASK:
            ref = (intent.task_ref or "").strip()
            task = self.registry.get_by_ref(ref) if ref else None
            if not task:
                message = "I couldn't find that task to cancel."
            elif task.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                message = f"That task is already {task.status.value}."
            else:
                ok = self.workers.cancel(task.id)
                if ok:
                    message = "Cancel requested."
                else:
                    refreshed = self.registry.get(task.id)
                    if refreshed and refreshed.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                        self.registry.mark_cancelled(task.id, "Cancelled by user")
                        message = "Task marked cancelled."
                    else:
                        final_status = refreshed.status.value if refreshed else task.status.value
                        message = f"That task is already {final_status}."

        # IntentAction.NONE = pure chat, no modification needed
        safe_message = (message or "").strip()
        if safe_message:
            return safe_message

        # Hard safety net: never return empty user-facing content.
        if intent.action == IntentAction.SPAWN_TASK:
            return "Working on it."
        if intent.action == IntentAction.CHECK_STATUS:
            return "I couldn't fetch task status right now."
        if intent.action == IntentAction.CANCEL_TASK:
            return "I couldn't process that cancel request right now."
        return "Sorry, I had trouble generating a response just now. Please try again."

    def _spawn_security_scan(self, channel: str, chat_id: str) -> Task | None:
        """Spawn a deterministic security scan using the shared scan builder.

        This ensures chat-triggered scans go through the exact same path as
        dashboard-triggered scans — same commands, same report format, same
        issue tracker integration.
        """
        from kyber.security.scan import build_scan_description

        try:
            description, _report_path = build_scan_description()

            task = self.registry.create(
                description=description,
                label="Full Security Scan",
                origin_channel=channel,
                origin_chat_id=chat_id,
                complexity="complex",
            )
            self.workers.spawn(task)
            return task
        except Exception as e:
            logger.error(f"Failed to spawn deterministic security scan: {e}")
            return None

    async def _completion_loop(self) -> None:
        """
        Background loop that delivers task completions to the user.

        Uses templates for all notifications — zero LLM voice calls.
        """
        while self._running:
            try:
                task: Task | None = None

                task = await asyncio.wait_for(self.workers.get_completion(), timeout=1.0)

                ref = task.completion_reference or "✅"

                try:
                    if task.status == TaskStatus.COMPLETED:
                        # Worker result IS the final answer — already in character voice
                        # from the persona prompt. Send it directly.
                        base = (task.result or "").strip()
                        if not base:
                            import random
                            templates = [
                                "Done! Finished up {label}.",
                                "All set — {label} is wrapped up.",
                                "Got it done — {label}.",
                            ]
                            base = random.choice(templates).format(label=task.label)
                        notification = base
                    else:
                        # Failures/cancellations — short template, no LLM call
                        import random
                        if task.status == TaskStatus.FAILED:
                            error = task.error or "unknown error"
                            fail_templates = [
                                "❌ {label} failed — {error}",
                                "❌ Hit a problem with {label}: {error}",
                                "❌ {label} didn't make it — {error}",
                            ]
                            notification = random.choice(fail_templates).format(
                                label=task.label, error=error,
                            )
                        elif task.status == TaskStatus.CANCELLED:
                            notification = f"🚫 {task.label} was cancelled."
                        else:
                            notification = f"{task.label} — {task.status.value}"
                except Exception as e:
                    # Template-based notifications shouldn't fail, but just in case
                    logger.error(f"Failed to build notification for '{task.label}': {e}")
                    notification = f"Task '{task.label}' — {task.status.value}"

                notification = _strip_task_refs_for_chat(notification)
                if not notification.strip():
                    notification = f"Task '{task.label}' — {task.status.value}"

                # Persist completion outputs into conversation history so terse
                # follow-ups ("proceed", "do it") still have full context.
                self._persist_completion_context(task, notification)

                try:
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=task.origin_channel,
                        chat_id=task.origin_chat_id,
                        content=notification,
                        is_background=True,
                    ))
                except Exception as e:
                    logger.error(f"Failed to publish completion for '{task.label}': {e}")
                    continue

                logger.info(f"Sent completion notification for: {task.label}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                if task is not None:
                    logger.error(f"Unexpected error in completion loop for '{task.label}': {e}")
                else:
                    logger.error(f"Error in completion loop: {e}")

    async def _progress_loop(self) -> None:
        """
        Background loop that sends batched progress updates.

        The LiveNarrator handles real-time updates for active tasks.
        This loop only fires for tasks that somehow aren't covered by
        the narrator (e.g., tasks spawned before narrator was started),
        and uses simple templates instead of LLM voice generation.
        """
        while self._running:
            try:
                interval = 10.0
                raw_interval = (os.getenv("KYBER_PROGRESS_UPDATE_INTERVAL_SECONDS", "") or "").strip()
                if raw_interval:
                    try:
                        interval = max(3.0, float(raw_interval))
                    except ValueError:
                        interval = 10.0
                await asyncio.sleep(interval)

                active_tasks = self.registry.get_active_tasks()
                if not active_tasks:
                    continue

                # Group by origin (channel:chat_id)
                by_origin: dict[str, list[Task]] = {}
                for task in active_tasks:
                    # Skip tasks the narrator is already covering
                    if self.narrator.is_narrating(task.id):
                        continue
                    key = f"{task.origin_channel}:{task.origin_chat_id}"
                    if key not in by_origin:
                        by_origin[key] = []
                    by_origin[key].append(task)

                for origin_key, tasks in by_origin.items():
                    if origin_key == "dashboard:dashboard":
                        continue

                    last_update = self._last_progress_update.get(origin_key)
                    if last_update:
                        elapsed = (datetime.now() - last_update).total_seconds()
                        if elapsed < interval * 0.8:
                            continue

                    def _progress_emoji(action: str) -> str:
                        a = (action or "").lower()
                        if any(w in a for w in ("read", "load", "check", "inspect", "look", "review", "scan")):
                            return "📖"
                        if any(w in a for w in ("write", "create", "save", "generate", "add")):
                            return "✏️"
                        if any(w in a for w in ("edit", "update", "modify", "fix", "patch", "replace")):
                            return "🔧"
                        if any(w in a for w in ("run", "exec", "install", "build", "deploy", "test")):
                            return "🏃"
                        if any(w in a for w in ("search", "find", "query")):
                            return "🔍"
                        if any(w in a for w in ("fetch", "download", "open", "browse", "url")):
                            return "🌐"
                        if any(w in a for w in ("send", "post", "publish", "notify", "message")):
                            return "💬"
                        return "⚙️"

                    summaries = []
                    for task in tasks:
                        if task.status == TaskStatus.RUNNING:
                            action = task.current_action or "working..."
                            step = f"step {task.iteration}"
                            emoji = _progress_emoji(action)
                            summaries.append(f"{emoji} {task.label} — {action} ({step})")

                    if not summaries:
                        continue

                    prev_summaries = self._last_progress_summary.get(origin_key)
                    if prev_summaries == summaries:
                        continue
                    self._last_progress_summary[origin_key] = summaries

                    # Use simple template — no LLM voice call
                    update = "\n".join(summaries)

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
