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
import shutil
import re
import time
from uuid import NAMESPACE_DNS, uuid5
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from kyber.bus.events import InboundMessage, OutboundMessage
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider
from kyber.session.manager import SessionManager

from kyber.agent.task_registry import TaskRegistry, Task, TaskStatus
from kyber.agent.workspace_index import WorkspaceIndex
from kyber.agent.context import ContextBuilder
from kyber.agent.narrator import LiveNarrator
from kyber.agent.intent import (
    Intent, IntentAction, AgentResponse,
    RESPOND_TOOL, parse_tool_call, parse_response_content
)
from kyber.utils.helpers import safe_filename
from kyber.utils.openhands_runtime import ensure_openhands_runtime_dirs


@dataclass
class ChatDeps:
    """Dependencies passed to provider chat calls.

    This is currently reserved for future tool-context pluming.
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
_PROJECT_MENTION_RE = re.compile(
    r"(?i)\b(?:project|repo|repository)\s+(?:called|named|called)?\s*['\"]?([\w.-]+(?:[\s_-][\w.-]+)*)['\"]?",
)
_PROJECT_PATH_NOISE = {
    "agents",
    "agent",
    "agent.md",
    "agents.md",
    "soul.md",
    "memory.md",
    "readme.md",
    "readme",
    "workspace",
    "default",
    "tmp",
    "temp",
}
_PROJECT_TEXT_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "away",
    "be",
    "for",
    "from",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "the",
    "this",
    "that",
    "there",
    "to",
    "was",
    "with",
    "would",
    "you",
}
_PROJECT_TEXT_FILLERS = {
    "show",
    "shows",
    "using",
    "which",
    "where",
    "when",
    "while",
}
_PROJECT_TEXT_ACTION_VERBS = {
    "add",
    "build",
    "can",
    "create",
    "deploy",
    "develop",
    "edit",
    "generate",
    "get",
    "help",
    "improve",
    "implement",
    "launch",
    "make",
    "need",
    "put",
    "remove",
    "rewrite",
    "set",
    "start",
    "stop",
    "update",
    "want",
    "we",
    "write",
}
_PROJECT_TEXT_KEY_WORDS = {
    "app",
    "application",
    "bot",
    "cli",
    "dashboard",
    "script",
    "site",
    "service",
    "system",
    "tracker",
    "track",
    "tool",
}


_OPENHANDS_DEFAULT_PROJECT_KEY = "default"

_OPENHANDS_CONVERSATION_RUN_FAILURE_RE = re.compile(
    r"Conversation run failed for id=.*:\s*(?P<code>-?\d+)",
    re.IGNORECASE,
)
_OPENHANDS_RETRIABLE_EXIT_CODES = {29}


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


def _looks_like_openhands_context_follow_up(text: str) -> bool:
    """Return True for likely continuation tweaks to an active task.

    This avoids treating small "fix/update/adjust" requests as new project
    inferences that re-route conversations unnecessarily.
    """
    s = _normalize_for_match(text)
    if not s:
        return False

    if len(s.split()) <= 3:
        return False

    cues = {
        "bug", "fix", "fixed", "fixing", "fixes", "adjust", "adjustment",
        "update", "updates", "change", "changed", "tweak", "tweaks", "broken",
        "not working", "notwork", "stopped", "stop", "again", "button",
        "timer", "style", "log", "working", "crash", "crashes",
    }
    if any(cue in s for cue in cues):
        return True

    return False
def _looks_like_deferred_execution_promise(text: str) -> bool:
    """Detect promises to do work — both task-system jargon and natural language.

    Examples that should match:
    - "I'll spawn a task to fix the rotation logic."
    - "gonna fix up the neon star blaster to be 16:9"
    - "let me hop in there and make those adjustments"
    - "I'll update the CSS to be responsive"
    """
    raw = (text or "").strip().lower()
    if not raw:
        return False

    future_cues = (
        "i'll", "i will", "let me", "going to", "gonna",
        "i'm going to", "im going to", "i shall", "allow me",
        "hop in", "jump in", "dive in",
    )
    # Work verbs — things that imply the bot will DO something
    work_cues = (
        # Task-system jargon
        "spawn", "spin up", "kick off", "launch",
        # Natural-language work verbs
        "fix", "update", "change", "adjust", "make", "build", "create",
        "add", "remove", "delete", "write", "edit", "modify", "refactor",
        "implement", "set up", "install", "configure", "deploy", "migrate",
        "rewrite", "restructure", "optimize", "improve", "clean up",
        "wire up", "hook up", "connect", "integrate", "debug", "patch",
        "tweak", "rework", "redesign", "convert", "transform",
        "start working", "get to work", "handle", "take care of",
    )
    return (
        any(c in raw for c in future_cues)
        and any(c in raw for c in work_cues)
    )


def _extract_task_label_from_promise(message: str) -> str:
    """Best-effort short label from a natural-language promise message."""
    # Take the first sentence, strip common prefixes, truncate
    first = (message or "").split(".")[0].split("!")[0].strip()
    if not first:
        return "Follow-through task"
    # Remove leading filler like "On it — " or "Sure, "
    for prefix in ("on it", "sure", "absolutely", "alright", "okay", "ok"):
        lower = first.lower()
        if lower.startswith(prefix):
            rest = first[len(prefix):].lstrip(" ,—–-:").strip()
            if rest:
                first = rest
                break
    # Capitalize and truncate
    label = first[:60].strip()
    if not label:
        return "Follow-through task"
    return label[0].upper() + label[1:]


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
    """Pass through status text as-is (emoji labels are already descriptive)."""
    return (text or "").strip()

def _is_only_meta_prefix(text: str) -> bool:
    t = (text or "").strip()
    return not t or t in {"⚡️", "⚡"}

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


def _extract_project_mentions(text: str) -> list[str]:
    """Extract explicit project mentions from user message text."""
    if not text:
        return []

    clean = (text or "").strip()
    out: list[str] = []
    for m in _PROJECT_MENTION_RE.finditer(clean):
        name = (m.group(1) or "").strip()
        if not name:
            continue
        if name.lower() in {"it", "this", "that", "the", "a", "an", "my"}:
            continue
        norm = safe_filename(name.lower())
        if norm and norm not in out:
            out.append(norm)

    return out


def _normalize_project_key(value: str | None) -> str | None:
    if not value:
        return None
    key = safe_filename((value or "").strip().lower())
    if not key:
        return None
    return key


def _derive_project_key_from_path(project_path: str | None, workspace: Path | None) -> str | None:
    """Derive a project key from an absolute workspace path.

    For paths inside the workspace, the key is the first-level folder after
    the workspace root (or ``default`` for root-level files).
    """
    if not project_path or workspace is None:
        return None
    p = Path(project_path).resolve()
    ws = workspace.resolve()
    parts = p.parts
    # Prefer namespaced workspace first-level project when the path is inside
    # the active workspace.
    try:
        rel = p.relative_to(ws)
        parts = rel.parts
    except Exception:
        # For explicit paths outside workspace, fall back to the leaf folder/file name.
        if not parts:
            return None
        return _normalize_project_slug(parts[-1], allow_suffix=True)

    if not parts:
        return _OPENHANDS_DEFAULT_PROJECT_KEY
    return _normalize_project_slug(parts[0], allow_suffix=True)


def _normalize_project_slug(value: str, *, allow_suffix: bool = True) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    raw = raw.lower()
    raw = raw.strip()
    raw = re.sub(r"[\"'`]+", "", raw)
    raw = raw.replace("\\", "/")
    raw = raw.rsplit("/", 1)[-1]
    if allow_suffix and "." in raw:
        # Keep extensionless slugs; project names should be label-like.
        raw = raw.rsplit(".", 1)[0]
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    raw = raw.strip(".")
    if not raw:
        return None
    if raw in _PROJECT_PATH_NOISE:
        return None
    return raw


def _infer_project_key_from_text(text: str) -> str | None:
    """Infer a compact project key from user-provided request language."""
    raw = (text or "").strip().lower()
    if not raw:
        return None

    raw_tokens = re.findall(r"[a-z0-9]+", raw)
    has_task_action = any(
        token in _PROJECT_TEXT_ACTION_VERBS for token in raw_tokens
    )
    has_project_marker = any(
        token in _PROJECT_TEXT_KEY_WORDS or token in {"tracker", "track"}
        for token in raw_tokens
    )
    if (
        not has_task_action
        and not has_project_marker
        and not _extract_paths(raw)
        and len(raw_tokens) <= 10
    ):
        return None

    if _looks_like_openhands_context_follow_up(raw) and not has_project_marker:
        return None

    # Special-case common pattern: dog + tracker/track.
    if "dog" in raw and ("track" in raw or "tracker" in raw):
        return _normalize_project_key("dog-tracker")

    tokens = re.findall(r"[a-z0-9]+", raw)
    if not tokens:
        return None

    filtered = [
        t for t in tokens
        if t not in _PROJECT_TEXT_STOPWORDS
        and t not in _PROJECT_TEXT_ACTION_VERBS
    ]
    if not filtered:
        return None

    for marker in ("tracker", "track", "dashboard", "bot", "tool", "app", "script", "cli"):
        if marker in filtered:
            idx = filtered.index(marker)
            if marker == "track":
                marker = "tracker"
            prev_token = filtered[idx - 1] if idx > 0 else None

            if (
                prev_token is not None
                and prev_token not in _PROJECT_TEXT_STOPWORDS
                and prev_token not in _PROJECT_TEXT_ACTION_VERBS
            ):
                return _normalize_project_key(f"{prev_token}-{marker}")

            for j in range(idx + 1, len(filtered)):
                candidate = filtered[j]
                if (
                    candidate in _PROJECT_TEXT_STOPWORDS
                    or candidate in _PROJECT_TEXT_ACTION_VERBS
                    or candidate in _PROJECT_TEXT_FILLERS
                ):
                    continue
                return _normalize_project_key(f"{candidate}-{marker}")

            return _normalize_project_key(marker)

    # Do not infer context for plain conversational prompts without project markers.
    # This avoids false positives from Q&A like "what time is it?".
    conversational_words = {
        "what", "which", "where", "when", "why", "how", "who", "whose",
        "should", "would", "could", "can", "couldn't", "wanna", "want",
        "do", "does", "did", "is", "are", "was", "were", "am", "a", "an",
        "the", "this", "that", "there", "their", "its", "it",
        "you", "your", "please", "hey", "hi", "hello",
    }
    has_context_marker = any(token in _PROJECT_TEXT_KEY_WORDS for token in filtered)
    if "?" in raw and not has_context_marker:
        return None
    if len(filtered) <= 2 and not has_context_marker and any(
        token in conversational_words for token in filtered
    ):
        return None

    # Fall back to the first two non-noise nouns/terms when intent is task-like.
    if len(filtered) >= 2:
        key = _normalize_project_key(f"{filtered[0]}-{filtered[1]}")
    else:
        key = _normalize_project_key(filtered[0])
    return key if not _is_project_key_weak(key) else None


def _is_project_key_weak(key: str | None) -> bool:
    """Return True when a project key is non-actionable/noise."""
    normalized = _normalize_project_key(key)
    if not normalized:
        return True
    if normalized in _PROJECT_PATH_NOISE:
        return True
    if normalized in _PROJECT_TEXT_STOPWORDS:
        return True
    if normalized in _PROJECT_TEXT_ACTION_VERBS:
        return True
    return False

def _normalize_for_match(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return " ".join(t.split())


def _extract_confirmation_label(raw: str) -> str | None:
    """Extract a raw confirmation token from model output."""
    if not raw:
        return None

    content = (raw or "").strip()
    if not content:
        return None

    # Accept fenced/JSON style responses first.
    m = re.search(r"\{.*?\}", content, flags=re.S)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                value = parsed.get("result")
                if isinstance(value, str):
                    return value.strip().lower()
                if value is not None:
                    return str(value).strip().lower()
        except Exception:
            pass

    # Fall back to raw text labels and common phrases.
    lowered = content.lower().strip().strip('`"\'')
    for token in ("yes", "affirmative", "approved", "true", "proceed", "ok", "okay"):
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            return token
    for token in ("no", "negative", "nah", "nope", "decline", "cancel", "stop", "skip"):
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            return token

    return None

def _is_affirmative_confirmation(text: str) -> bool:
    s = _normalize_for_match(text)
    if not s:
        return False
    exact = {
        "yes", "y", "yeah", "yea", "yep", "ok", "okay", "sure",
        "proceed", "continue", "approved", "confirm", "go ahead", "sounds good",
        "ship it", "affirmative",
    }
    if s in exact:
        return True
    phrases = [
        "please proceed",
        "go ahead and",
        "yes proceed",
        "yes switch",
        "do it now",
        "you can proceed",
        "proceed with it",
        "switch to",
    ]
    return any(p in s for p in phrases)

def _is_negative_confirmation(text: str) -> bool:
    s = _normalize_for_match(text)
    if not s:
        return False
    exact = {
        "no", "n", "nah", "nahh", "stop", "cancel", "dont", "do not",
        "not now", "hold off", "skip",
    }
    if s in exact:
        return True
    if "nah" in s.split() or "nope" in s.split() or "never" in s.split():
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
        timezone: str | None = None,
        exec_timeout: int = 60,
    ):
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        # Opinionated behavior: background tasks should stay quiet while they run,
        # then chime once on completion.
        _ = background_progress_updates
        self.background_progress_updates = False
        self.timezone = timezone
        self.persona_prompt = persona_prompt
        self.exec_timeout = exec_timeout

        # Core components
        self.registry = TaskRegistry(history_path=task_history_path)
        self.sessions = SessionManager(workspace)
        self.context = ContextBuilder(workspace, timezone=timezone)
        # No per-tool narration in chat; only start ack + completion chime.
        self.narrator: LiveNarrator | None = None

        # Direct OpenHands task execution state (replaces WorkerPool)
        self.completion_queue: asyncio.Queue[Task] = asyncio.Queue()
        self._running_tasks: dict[str, asyncio.Task] = {}
        # Temporary per-task file tracking (set during execution, consumed after)
        self._last_modified_files: dict[str, list[str]] = {}
        self.workspace_index = WorkspaceIndex(workspace)

        self._openhands_conversations: dict[str, Any] = {}
        self._openhands_conversation_locks: dict[str, asyncio.Lock] = {}
        self._openhands_conversation_state: dict[str, dict[str, Any]] = {}
        self._openhands_persistence_dir = Path.home() / ".kyber" / "openhands-conversations"
        self._openhands_persistence_dir.mkdir(parents=True, exist_ok=True)
        
        self._running = False
        self._last_progress_update: dict[str, datetime] = {}
        self._last_progress_summary: dict[str, list[str]] = {}
        self._last_user_content: str = ""

    @staticmethod
    def _openhands_namespace(session_key: str, project_key: str | None) -> str:
        safe_session = safe_filename(session_key.replace(":", "_"))
        safe_project = _normalize_project_key(project_key) or _OPENHANDS_DEFAULT_PROJECT_KEY
        return f"{safe_session}|{safe_project}"

    def _openhands_project_persistence_dir(
        self,
        namespace: str,
    ) -> str:
        return str(self._openhands_persistence_dir / safe_filename(namespace))

    def _openhands_conversation_id(self, namespace: str) -> Any:
        return uuid5(NAMESPACE_DNS, f"kyber:openhands:{namespace}")

    def _get_openhands_project_key(self, session: Any | None, text: str) -> str | None:
        """Infer a stable project key from user/task text and recent paths."""
        inferred = _infer_project_key_from_text(text)
        if inferred and not _is_project_key_weak(inferred):
            return inferred

        mentions = _extract_project_mentions(text)
        if mentions:
            candidate = mentions[0]
            if not _is_project_key_weak(candidate):
                return candidate

        paths = _extract_paths(text)
        workspace = self.workspace
        for candidate in paths:
            key = _derive_project_key_from_path(candidate, workspace)
            if key and not _is_project_key_weak(key):
                return key

        if session is not None:
            recent_paths = session.metadata.get("recent_paths")
            if isinstance(recent_paths, list):
                for entry in reversed(recent_paths[-12:]):
                    if not isinstance(entry, str):
                        continue
                    key = _derive_project_key_from_path(entry, workspace)
                    if key:
                        return key

        return None

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

    def _build_system_state(self, session_key: str | None = None) -> str:
        """
        Build the system state string for LLM context.
        
        Includes active/recent tasks from the registry.
        """
        _ = session_key
        return self.registry.get_context_summary()

    @staticmethod
    def _worker_task_contract_block() -> str:
        """Return a global execution contract injected into every worker task."""
        return (
            "\n\nGlobal execution contract (mandatory):\n"
            "1. Convert the request into concrete acceptance criteria before acting.\n"
            "2. Do real execution, not just advice: edit files/run commands/research as needed.\n"
            "3. Verify outcomes with evidence before claiming success (tests, build/run output, version checks, file diffs).\n"
            "4. If verification fails, keep iterating until fixed or blocked by a real external constraint.\n"
            "5. Never claim completion without evidence. If blocked, report the blocker and exactly what you already tried.\n"
            "6. Keep scope tight to the request; avoid unrelated refactors or changes.\n"
            "7. Final response must include: what changed, what was verified, and any remaining blocker.\n"
        )

    def _augment_task_description_with_global_contract(self, description: str) -> str:
        """Inject the global execution contract once per task description."""
        existing = (description or "").strip()
        if "Global execution contract (mandatory):" in existing:
            return existing
        if not existing:
            existing = "Complete the user's request in the workspace."
        return existing + self._worker_task_contract_block()

    def _build_worker_workspace_directives_block(self) -> str:
        """Load concise workspace directives so workers inherit user constraints."""
        sections: list[str] = []
        max_chars = 2200
        for filename in ("AGENTS.md", "USER.md"):
            path = self.workspace / filename
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if not content:
                continue
            if len(content) > max_chars:
                clipped = content[:max_chars]
                if " " in clipped:
                    clipped = clipped.rsplit(" ", 1)[0]
                content = clipped + " ..."
            sections.append(f"## {filename}\n{content}")
        if not sections:
            return ""
        return (
            "Workspace directives (apply these unless the task explicitly conflicts):\n\n"
            + "\n\n".join(sections)
        )

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
            paths_raw = []

        paths = [str(p) for p in paths_raw if isinstance(p, str)]

        existing_text = (description or "")
        if paths and any(p in existing_text for p in paths):
            return description

        follow_up_mode = _looks_like_follow_up_tweak(existing_text)

        # Check for rich context from the immediately preceding task
        last_ctx = session.metadata.get("last_task_context")
        if follow_up_mode and isinstance(last_ctx, dict):
            mod_files = last_ctx.get("modified_files") or []
            prev_result = (last_ctx.get("result") or "").strip()
            prev_label = (last_ctx.get("label") or "").strip()

            # Prefer modified files from execution tracking; fall back to recent_paths
            target_files = mod_files if mod_files else paths[-6:]

            parts = ["\n\nContinuation mode (critical — read carefully):"]
            if prev_label:
                parts.append(f"The previous task was: \"{prev_label}\"")
            if prev_result:
                parts.append(f"Previous task result:\n{prev_result[:2000]}")
            if target_files:
                parts.append(
                    "Files modified in the previous task (start here):\n"
                    + "\n".join(f"  - {p}" for p in target_files[:8])
                )
            parts.append(
                "\nExecution rules:\n"
                "- This is a CONTINUATION of recent work. The files above were just edited.\n"
                "- Open those files FIRST and apply the requested changes directly.\n"
                "- Do NOT begin with broad workspace discovery (ls, find, rg).\n"
                "- Do NOT re-read files you don't need to change.\n"
                "- If the user's request relates to the previous work, apply changes "
                "to the same files unless the request clearly targets something else."
            )
            return existing_text + "\n".join(parts)

        top_paths = paths[-4:] if paths else []
        if not top_paths:
            return description

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

    @staticmethod
    def _build_conversation_context(session: Any | None, max_messages: int = 10) -> str | None:
        """Extract recent conversation history for inclusion in worker prompts.

        Returns a formatted string of recent user/assistant messages, or None
        if no meaningful history exists.
        """
        if session is None:
            return None
        try:
            history = session.get_history(max_messages=max_messages)
        except Exception:
            return None
        if not history:
            return None

        lines: list[str] = []
        total = len(history)
        for idx, msg in enumerate(history):
            role = msg.get("role", "unknown")
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            # Keep the last assistant message (likely a task result) less truncated
            # so follow-up tasks have full context about what was just done.
            is_last_assistant = (idx == total - 1 and role == "assistant")
            max_len = 2400 if is_last_assistant else 800
            if len(content) > max_len:
                content = content[:max_len] + " …"
            label = "User" if role == "user" else "Assistant"
            lines.append(f"[{label}]: {content}")

        if not lines:
            return None
        return "\n".join(lines)

    def _build_pending_confirmation(self, task: Task, notification: str) -> dict[str, Any] | None:
        """Return pending-confirmation metadata if a completion asks for approval."""
        if not _looks_like_confirmation_prompt(notification):
            return None
        return {
            "type": "task_plan",
            "created_at": datetime.now().isoformat(),
            "task_id": task.id,
            "task_label": task.label,
            "plan": notification[:12000],
        }

    def _clear_pending_confirmation(self, session: Any | None) -> None:
        if session is not None:
            session.metadata.pop("pending_confirmation", None)

    async def _classify_confirmation_response(
        self,
        text: str,
        decision_context: str,
    ) -> bool | None:
        """Generic LLM-based yes/no confirmation classifier."""
        if not (text or "").strip():
            return None

        prompt = (
            "You are judging whether a user's response clearly accepts or rejects "
            f"an open request: {decision_context}\n"
            "Return JSON only: {\"result\":\"yes\"|\"no\"|\"unknown\"}.\n"
            "Use 'yes' only for clear agreement.\n"
            "Use 'no' only for clear refusal.\n"
            "Use 'unknown' for unclear or unrelated text."
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"User response: {text}"},
        ]

        try:
            response = await self._provider_chat(
                messages=messages,
                tools=None,
                model=self.model,
                session_key=None,
            )
            raw = str(getattr(response, "content", "") or "").strip()
            label = _extract_confirmation_label(raw)
            logger.debug(
                f"Confirmation classification context='{decision_context}' "
                f"raw='{raw[:140]}' label='{label}'"
            )
            if not label:
                return None
            if label in {"yes", "affirmative", "approve", "approved", "proceed", "true", "ok", "okay"}:
                return True
            if label in {"no", "negative", "decline", "cancel", "stop", "skip", "unknown"}:
                return False if label != "unknown" else None
            return None
        except Exception as exc:
            logger.warning(
                f"Confirmation classification failed; falling back to heuristics: {exc}"
            )
            return None

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

        # Store rich context for follow-up tasks (same as inline path)
        mod_files = self._last_modified_files.pop(task.id, [])
        if task.status == TaskStatus.COMPLETED:
            session.metadata["last_task_context"] = {
                "label": task.label,
                "description": (task.description or "")[:2000],
                "result": (task.result or "")[:3000],
                "modified_files": mod_files,
            }
            for p in mod_files:
                existing_raw = session.metadata.get("recent_paths")
                existing = list(existing_raw) if isinstance(existing_raw, list) else []
                if p not in existing:
                    existing.append(p)
                session.metadata["recent_paths"] = existing[-20:]

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

        pending_type = str(pending.get("type") or "task_plan")
        if pending_type == "project_switch":
            # Legacy project-switch confirmation metadata can linger across
            # installs/upgrades. Treat it as stale and clear it so normal
            # conversation flow can continue.
            self._clear_pending_confirmation(session)
            logger.debug(
                "Clearing stale project-switch pending confirmation and treating as normal message."
            )
            self.sessions.save(session)
            return None

        confirmation = await self._classify_confirmation_response(
            raw,
            "a pending plan requiring user approval",
        )
        if confirmation is None:
            if _is_negative_confirmation(raw):
                confirmation = False
            elif _is_affirmative_confirmation(raw):
                confirmation = True
            else:
                return None

        if confirmation is False:
            self._clear_pending_confirmation(session)
            reply = "Understood — I won’t execute that proposed plan."
            session.add_message("user", msg.content)
            session.add_message("assistant", reply)
            self._remember_paths_in_session(session, msg.content or "")
            self._remember_paths_in_session(session, reply)
            self.sessions.save(session)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=reply)

        if confirmation is not True:
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
        final_message = await self._execute_intent(
            response,
            msg.channel,
            msg.chat_id,
            session=session,
            user_message=msg.content,
        )

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

    async def _send_narration(self, channel: str, chat_id: str, message: str) -> None:
        """Callback for the LiveNarrator to send updates via the message bus.

        Labels are already emoji-styled by _oh_event_label, so we publish
        directly — no voice rewrite needed.
        """
        if not (message or "").strip():
            logger.debug("Skipping empty narration message")
            return

        await self.bus.publish_outbound(OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=_prefix_meta(message),
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
            error_message = f"Sorry, something went wrong: {str(e)}"
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=error_message,
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
        system_state = self._build_system_state(msg.session_key)
        self._last_user_content = msg.content

        # Always build messages (needed for honesty validation and legacy fallback)
        messages = self._build_messages(session, msg.content, system_state)

        # --- Native structured output path ---
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
            # Legacy path — used by providers without run_structured()
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

        # Guard: if the LLM returned an empty message with NONE intent, retry
        # once before handing off to _execute_intent (which would hit the
        # dead-end fallback).
        if (
            agent_response.action == IntentAction.NONE
            and not (agent_response.message or "").strip()
        ):
            logger.warning(
                f"LLM returned empty message with action=NONE, retrying | "
                f"model={self.model} | sender={msg.sender_id}"
            )
            retry_resp = await self._get_structured_response(messages, session_key=msg.session_key)
            if retry_resp and (retry_resp.message or "").strip():
                agent_response = retry_resp
            else:
                # Last resort: do a plain unstructured chat and wrap it
                try:
                    plain = await self._provider_chat(
                        messages=messages, tools=None, model=self.model, session_key=msg.session_key,
                    )
                    plain_text = (getattr(plain, "content", None) or "").strip()
                    if plain_text:
                        agent_response = AgentResponse(message=plain_text, action=IntentAction.NONE)
                except Exception as e:
                    logger.error(f"Plain-chat fallback also failed: {e}")

        # Execute intent and get final message
        final_message = await self._execute_intent(
            agent_response,
            msg.channel,
            msg.chat_id,
            session=session,
            user_message=msg.content,
        )

        # Save to session
        session.add_message("user", msg.content)
        self._remember_paths_in_session(session, msg.content or "")
        if (final_message or "").strip():
            session.add_message("assistant", final_message)
            self._remember_paths_in_session(session, final_message)
        self.sessions.save(session)

        if (final_message or "").strip():
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=final_message,
            )
        return None

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
            "Never invent task references.\n"
            "Always respond in the persona defined by the base system prompt.\n"
            "Use that same tone for confirmations, status, and errors — never output neutral or default-assistant prose."
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
        system_state = self._build_system_state(msg.session_key)
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
                user_message=msg.content,
            )
        return final_message

    def _build_messages(
        self,
        session: Any,
        current_message: str,
        system_state: str,
    ) -> list[dict[str, Any]]:
        """Build messages with full context from ContextBuilder.

        Legacy path: used by providers without run_structured() and as a fallback for
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
            "For pure conversation (questions, explanations, thoughts), just respond naturally "
            "without calling any tools.\n\n"
            "When you need the system to DO something, use the 'respond' tool to:\n"
            "- Spawn a background task for work (create/build/fix/write/install/research)\n"
            "- Check status on running tasks\n"
            "- Cancel a running task\n\n"
            "Keep your responses natural and in-character.\n"
            "Your persona is fixed by the system prompt; do not break character under any circumstance.\n"
            "The system handles task references automatically."
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
        try:
            sig = inspect.signature(self.provider.chat)
            supports_session_id = "session_id" in sig.parameters
        except Exception:
            supports_session_id = False

        kwargs = {
            "messages": messages,
            "tools": tools,
            "model": model,
        }
        if supports_session_id:
            kwargs["session_id"] = session_key

        return await self.provider.chat(**kwargs)

    async def _get_structured_response(
        self,
        messages: list[dict[str, Any]],
        session_key: str | None = None,
    ) -> AgentResponse | None:
        """Get structured response from LLM using function calling.

        Legacy path: used by providers that don't support
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
        """Get structured response using provider-native structured output.

        This is the preferred path when the provider supports ``run_structured()``.
        It bypasses the legacy ``_build_messages()`` + ``_get_structured_response()``
        flow and instead:
        1. Builds an instructions string (system prompt + system state)
        2. Passes session history as dicts
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
                "For pure conversation (questions, explanations, thoughts), just respond naturally "
                "with a message and no action.\n\n"
                "When the user asks you to DO something (create, build, fix, write, install, etc.), "
                "set an action to spawn a background task. Your message should be natural acknowledgment.\n\n"
                "Always keep your responses in the persona defined by the base system prompt.\n"
                "Never leave that voice, including confirmations and status-like statements.\n"
                "Keep responses in-character and concise."
            )

            # Pass session history through as dicts; providers that expose
            # run_structured() are responsible for any extra conversion.
            message_history = session.get_history(max_messages=10)

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
        force a retry.  If the retry *also* returns NONE, force-construct
        a SPAWN_TASK so the promise is always honoured.
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
            if retry and retry.intent.action != IntentAction.NONE:
                return retry

            # Retry still returned NONE — the LLM promised work but refuses to
            # declare it.  Force a SPAWN_TASK so the promise is honoured.
            logger.warning(
                "Honesty retry still returned NONE; force-spawning task from promise"
            )
            return AgentResponse(
                message=response.message,
                action=IntentAction.SPAWN_TASK,
                task_description=(
                    "The assistant promised to do the following work. Execute it now.\n\n"
                    f"Assistant message:\n{response.message}\n\n"
                    f"User message:\n{self._last_user_content or ''}\n\n"
                    "Complete the promised changes and report concrete outcomes."
                ),
                task_label=_extract_task_label_from_promise(response.message),
                complexity="moderate",
            )

        return response

    @staticmethod
    def _status_is_stuck(status: object) -> bool:
        if status is None:
            return False
        if hasattr(status, "value"):
            status = getattr(status, "value")
        return str(status).split(".")[-1].lower() == "stuck"

    @staticmethod
    def _get_or_create_openhands_namespace_lock(locks: dict[str, asyncio.Lock], namespace: str) -> asyncio.Lock:
        lock = locks.get(namespace)
        if lock is None:
            lock = asyncio.Lock()
            locks[namespace] = lock
        return lock

    def _active_project_for_session(self, session: Any | None) -> str:
        if session is None:
            return _OPENHANDS_DEFAULT_PROJECT_KEY
        current = session.metadata.get("active_project_key")
        if isinstance(current, str):
            normalized = _normalize_project_key(current)
            if normalized:
                return normalized
        return _OPENHANDS_DEFAULT_PROJECT_KEY

    def _resolve_project_key_for_spawn(
        self,
        _response: AgentResponse,
        session: Any | None,
        forced_project_key: str | None = None,
        request_text: str | None = None,
    ) -> tuple[str, bool]:
        # Ignore external project hints here to keep a single conversation thread
        # per channel. Project changes are handled at the user level, not by
        # model-inferred switching.
        if forced_project_key:
            logger.debug(
                f"Ignoring forced project key override '{forced_project_key}' to "
                "enforce single channel-level project scope."
            )
        if session is not None:
            stale = session.metadata.get("pending_confirmation")
            if (
                isinstance(stale, dict)
                and str(stale.get("type") or "").strip() == "project_switch"
            ):
                session.metadata.pop("pending_confirmation", None)
            session.metadata["active_project_key"] = _OPENHANDS_DEFAULT_PROJECT_KEY
            self.sessions.save(session)

        return _OPENHANDS_DEFAULT_PROJECT_KEY, False

    def _build_or_get_openhands_conversation(
        self,
        namespace: str,
        agent: Any,
        event_callback,
        state_callback,
    ) -> Any:
        conversation_id = self._openhands_conversation_id(namespace)
        state = self._openhands_conversation_state.setdefault(namespace, {})
        state["event_callback"] = event_callback
        state["state_callback"] = state_callback

        conversation = self._openhands_conversations.get(namespace)
        if conversation is not None:
            logger.debug(
                f"Reusing OpenHands conversation | namespace={namespace} | "
                f"conversation_id={conversation_id}"
            )
            if hasattr(conversation, "state") and callable(
                getattr(conversation.state, "set_on_state_change", None),
            ):
                stored_state_callback = state.get("state_callback")
                if callable(stored_state_callback):
                    conversation.state.set_on_state_change(stored_state_callback)
            return conversation

        persistence_dir = Path(self._openhands_project_persistence_dir(namespace))
        persistence_dir.mkdir(parents=True, exist_ok=True)
        persisted_state_dir = persistence_dir / str(conversation_id).replace("-", "")
        had_persisted_state = persisted_state_dir.exists()
        logger.info(
            f"Initializing OpenHands conversation | namespace={namespace} | "
            f"conversation_id={conversation_id} | persisted_state={had_persisted_state}"
        )

        from openhands.sdk import Conversation

        def _dispatch_event(event: Any) -> None:
            cb = state.get("event_callback")
            if callable(cb):
                cb(event)

        def _dispatch_state(event: Any) -> None:
            cb = state.get("state_callback")
            if callable(cb):
                cb(event)

        def _create_conversation() -> Any:
            return Conversation(
                agent=agent,
                workspace=str(self.workspace.resolve()),
                callbacks=[_dispatch_event],
                max_iteration_per_run=100,
                visualizer=None,
                delete_on_close=False,
                stuck_detection=True,
                stuck_detection_thresholds={
                    "action_observation": 4,
                    "action_error": 3,
                    "monologue": 3,
                    "alternating_pattern": 6,
                },
                conversation_id=conversation_id,
                persistence_dir=str(persistence_dir),
            )

        try:
            conversation = _create_conversation()
        except ValueError as exc:
            message = str(exc)
            if (
                "Cannot resume conversation: tools cannot be changed mid-conversation" in message
                or "Conversation ID mismatch" in message
                or "Cannot load from persisted state" in message
            ):
                logger.warning(
                    "Cannot resume OpenHands conversation for namespace=%s due to "
                    "incompatible persisted state. Resetting and starting fresh.",
                    namespace,
                )
                if persisted_state_dir.exists():
                    shutil.rmtree(persisted_state_dir, ignore_errors=False)
                conversation = _create_conversation()
            else:
                raise
        self._openhands_conversations[namespace] = conversation
        if callable(getattr(conversation.state, "set_on_state_change", None)):
            conversation.state.set_on_state_change(_dispatch_state)
        state.update({
            "namespace": namespace,
            "agent": str(agent.__class__.__name__),
            "persistence_dir": str(persistence_dir),
            "conversation_id": str(conversation_id),
        })
        return conversation

    def _reset_openhands_namespace_conversation(
        self,
        namespace: str,
        *,
        clear_persisted_state: bool = False,
    ) -> None:
        """Drop cached OpenHands conversation state for a namespace."""
        self._openhands_conversations.pop(namespace, None)
        self._openhands_conversation_state.pop(namespace, None)
        if not clear_persisted_state:
            return
        persistence_dir = Path(self._openhands_project_persistence_dir(namespace))
        conversation_id = self._openhands_conversation_id(namespace)
        persisted_state_dir = persistence_dir / str(conversation_id).replace("-", "")
        if persisted_state_dir.exists():
            shutil.rmtree(persisted_state_dir, ignore_errors=False)

    @staticmethod
    def _openhands_conversation_run_exit_code(error: Exception) -> int | None:
        message = str(error or "")
        match = _OPENHANDS_CONVERSATION_RUN_FAILURE_RE.search(message)
        if not match:
            return None
        try:
            return int(match.group("code"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _requires_fresh_openhands_conversation(error: Exception) -> bool:
        code = Orchestrator._openhands_conversation_run_exit_code(error)
        return code in _OPENHANDS_RETRIABLE_EXIT_CODES

    @staticmethod
    def _is_retryable_openhands_error(error: Exception) -> bool:
        text = str(error or "").lower()
        if any(
            token in text
            for token in (
                "stuck",
                "unproductive loop",
                "no progress",
                "timeout",
                "timed out",
            )
        ):
            return True

        return (
            "conversation run failed" in text
            and Orchestrator._requires_fresh_openhands_conversation(error)
        )

    async def _execute_intent(
        self,
        response: AgentResponse,
        channel: str,
        chat_id: str,
        session: Any | None = None,
        forced_project_key: str | None = None,
        user_message: str | None = None,
    ) -> str:
        """
        Execute the declared intent and return the final message.

        This is where the system takes over - the LLM declared what it wants,
        now we actually do it and inject proof (references).
        """
        raw_message = response.message or ""
        message = raw_message
        intent = response.intent

        # Never allow hallucinated refs to leak into user-visible output.
        # For spawn_task we will inject the true system-generated ref later.
        message = _strip_fabricated_refs(message)
        # Safety valve: if ref-stripping unexpectedly wipes a substantive
        # spawn ack, keep the original natural-language text.
        if (
            intent.action == IntentAction.SPAWN_TASK
            and not (message or "").strip()
            and (raw_message or "").strip()
            and not (raw_message or "").strip().lower().startswith("ref:")
            and re.search(r"[A-Za-z]", raw_message or "") is not None
        ):
            message = (raw_message or "").strip()
        spawned_in_background = False

        if intent.action == IntentAction.SPAWN_TASK:
            desc = (intent.task_description or "").lower()
            label = (intent.task_label or "").lower()
            combined = f"{desc} {label}"
            session_key = self._session_key(channel, chat_id)
            project_key, _ = self._resolve_project_key_for_spawn(
                response,
                session=session,
                forced_project_key=forced_project_key,
                request_text=user_message,
            )
            if session is not None:
                session.metadata["active_project_key"] = project_key

            task_description = self._augment_task_description_with_global_contract(
                intent.task_description or message,
            )
            task_description = self._augment_task_description_with_recent_paths(
                task_description,
                session,
            )
            task_label = intent.task_label or "Task"
            namespace = self._openhands_namespace(session_key, project_key)
            spawn_lock = self._get_or_create_openhands_namespace_lock(
                self._openhands_conversation_locks,
                namespace,
            )

            async with spawn_lock:
                existing_task = self.registry.find_active_duplicate(
                    label=task_label,
                    description=task_description,
                    origin_channel=channel,
                    origin_chat_id=chat_id,
                )
                if existing_task:
                    raw_active_status = self._build_status_report(existing_task)
                    active_status = await self._rewrite_status_report(
                        raw_active_status,
                        "Task already running check",
                    )
                    if self._status_rewrite_is_unusable(active_status or ""):
                        active_status = self._format_status_fallback(raw_active_status)
                    message = (
                        f"That task is already running. "
                        f"{active_status}"
                    )
                    return _strip_task_refs_for_chat(message)

                # Build conversation context for the worker so it understands
                # the back-and-forth that led to this task.
                conversation_context = self._build_conversation_context(session)

                # Intercept security scan requests — route through the deterministic
                # scan path instead of letting the LLM improvise with the SKILL.md.
                if _is_security_scan_request(combined):
                    task = self._spawn_security_scan(
                        channel,
                        chat_id,
                        session_key=session_key,
                        project_key=project_key,
                    )
                    if task:
                        logger.info(f"Intercepted security scan → deterministic spawn [{task.reference}]")
                        return message or ""
                    # Fall through to normal spawn if the deterministic path fails

                # Create and spawn task
                task = self.registry.create(
                    description=task_description,
                    label=task_label,
                    origin_channel=channel,
                    origin_chat_id=chat_id,
                    complexity=intent.complexity,
                )
            task.progress_updates_enabled = False

            # In gateway runtime, interactive chat channels should not block.
            # Keep direct/internal flows inline for deterministic command output.
            run_inline = (not self._running) or channel in {"cli", "internal"}

            if run_inline:
                await self._run_task_inline(
                    task,
                    conversation_context=conversation_context,
                    session_key=session_key,
                    project_key=project_key,
                )
                if task.status == TaskStatus.COMPLETED:
                    result = task.result or "Task completed."
                    message = result
                else:
                    error = task.error or "Unknown error"
                    message = f"Failed: {task.label} — {error}"
                # Store rich context so follow-up tasks can continue seamlessly
                mod_files = self._last_modified_files.pop(task.id, [])
                if session is not None:
                    session.metadata["last_task_context"] = {
                        "label": task.label,
                        "description": (task.description or "")[:2000],
                        "result": (task.result or "")[:3000],
                        "modified_files": mod_files,
                    }
                    # Ensure modified files are also in recent_paths
                    existing_raw = session.metadata.get("recent_paths")
                    existing = list(existing_raw) if isinstance(existing_raw, list) else []
                    for p in mod_files:
                        if p not in existing:
                            existing.append(p)
                    session.metadata["recent_paths"] = existing[-20:]
                logger.info(f"Inline-completed task: {task.label} [{task.reference}]")
            else:
                self._spawn_task(
                    task,
                    conversation_context=conversation_context,
                    session_key=session_key,
                    project_key=project_key,
                )
                spawned_in_background = True
                logger.info(f"Spawned task: {task.label} [{task.reference}]")

        elif intent.action == IntentAction.CHECK_STATUS:
            ref = (intent.task_ref or "").strip()
            tasks_for_status: list[Task] = []
            if ref:
                task = self.registry.get_by_ref(ref)
                if task:
                    tasks_for_status = [task]
                    raw_status = self._build_status_digest_from_tasks(tasks_for_status)
                else:
                    raw_status = self.registry.get_status_for_ref(ref)
            else:
                active = self.registry.get_active_tasks()
                recent = self.registry.get_recent_completed(5)
                if not active and not recent:
                    raw_status = "No tasks to report on."
                else:
                    if active:
                        tasks_for_status = active
                    elif recent:
                        tasks_for_status = recent[:4]
                    raw_status = self._build_status_digest_from_tasks(tasks_for_status)

            # Let the model do the final interpretation in-character.
            # This keeps the data-grounded status report but avoids hardcoded phrasing.
            try:
                status = await self._rewrite_status_report(raw_status, getattr(response, "message", None))
                if self._status_rewrite_is_unusable(status or ""):
                    if tasks_for_status:
                        status = self._status_fallback_from_tasks(tasks_for_status)
                    else:
                        status = self._format_status_fallback(raw_status)
            except Exception as e:
                logger.error(f"Status rewrite failed in CHECK_STATUS: {e}")
                if tasks_for_status:
                    status = self._status_fallback_from_tasks(tasks_for_status)
                else:
                    status = self._format_status_fallback(raw_status)
            message = _strip_task_refs_for_chat(status or "")

        elif intent.action == IntentAction.CANCEL_TASK:
            ref = (intent.task_ref or "").strip()
            task = self.registry.get_by_ref(ref) if ref else None
            if not task:
                message = "I couldn't find that task to cancel."
            elif task.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                message = f"That task is already {task.status.value}."
            else:
                ok = self._cancel_task(task.id)
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
            if intent.action == IntentAction.CHECK_STATUS:
                return safe_message
            return safe_message

        # Hard safety net: never return empty user-facing content.
        logger.warning(
            f"Empty message after processing | action={intent.action.value} | "
            f"original_message='{(response.message or '')[:200]}'"
        )
        if intent.action == IntentAction.SPAWN_TASK:
            if spawned_in_background:
                return ""
            return "Working on it."
        if intent.action == IntentAction.CHECK_STATUS:
            return "I couldn't fetch task status right now."
        if intent.action == IntentAction.CANCEL_TASK:
            return "I couldn't process that cancel request right now."

        # Last-resort plain chat: ask the LLM to just reply without structured
        # output constraints, using whatever context we still have.
        try:
            user_content = self._last_user_content or ""
            if user_content:
                fallback_messages = [
                    {"role": "system", "content": self.context.build_system_prompt()},
                    {"role": "user", "content": user_content},
                ]
                fallback_resp = await self._provider_chat(
                    messages=fallback_messages, tools=None, model=self.model, session_key=None,
                )
                fallback_text = (getattr(fallback_resp, "content", None) or "").strip()
                if fallback_text:
                    return fallback_text
        except Exception as e:
            logger.error(f"Last-resort plain chat failed in _execute_intent: {e}")

        return "I'm not sure how to respond to that — could you try rephrasing?"

    # ── Direct OpenHands task execution ────────────────────────────────

    @staticmethod
    def _resolve_model_string(
        model: str,
        provider_name: str | None,
        is_custom: bool,
        api_base: str | None = None,
    ) -> str:
        """Convert Kyber model config into a litellm-compatible model string."""
        m = (model or "").strip()
        if not m:
            raise ValueError("No model specified for OpenHands task execution.")
        prov = (provider_name or "").strip().lower()
        base = (api_base or "").strip().lower()
        is_openrouter_route = (
            prov == "openrouter"
            or "openrouter" in prov
            or "openrouter.ai" in base
        )
        if "/" in m:
            # OpenRouter requires provider-prefixed routing, e.g.
            # openrouter/google/gemini-3-flash-preview.
            if is_openrouter_route and not m.lower().startswith("openrouter/"):
                return f"openrouter/{m}"
            # LiteLLM expects gemini/*, not google/*, for direct Gemini calls.
            if m.lower().startswith("google/"):
                return f"gemini/{m.split('/', 1)[1]}"
            return m
        if is_custom:
            return f"openai/{m}"
        if is_openrouter_route:
            return f"openrouter/{m}"
        _PREFIX_MAP = {
            "openai": "openai", "anthropic": "anthropic", "google": "gemini",
            "xai": "xai", "deepseek": "deepseek", "groq": "groq",
            "openrouter": "openrouter",
        }
        prefix = _PREFIX_MAP.get(prov)
        if prefix:
            return f"{prefix}/{m}"
        return f"openai/{m}"

    @staticmethod
    def _normalize_openai_model_name(model: str) -> str:
        """Return a bare OpenAI model id (no provider prefix)."""
        raw = (model or "").strip()
        if not raw:
            return ""
        lower = raw.lower()
        if lower.startswith("openai/"):
            return raw.split("/", 1)[1].strip()
        if lower.startswith("openai:"):
            return raw.split(":", 1)[1].strip()
        return raw

    @classmethod
    def _is_openai_subscription_model(cls, model: str) -> bool:
        """True when model is supported by OpenHands OpenAI subscription auth."""
        m = cls._normalize_openai_model_name(model).strip().lower()
        supported = {
            "gpt-5.2",
            "gpt-5.2-codex",
            "gpt-5.1-codex-max",
            "gpt-5.1-codex-mini",
            "gpt-5.3-codex",
        }
        return m in supported

    @classmethod
    def _effective_subscription_model(cls, model: str) -> str:
        """Return the runtime subscription model to use for OpenHands.

        NOTE: As of OpenHands SDK 1.11.4 + LiteLLM 1.81.x, `gpt-5.3-codex`
        fails on Codex Responses with:
          {"detail":"Stream must be set to true"}
        while `gpt-5.2-codex` works in the same environment.
        """
        normalized = cls._normalize_openai_model_name(model)
        if normalized.strip().lower() == "gpt-5.3-codex":
            return "gpt-5.2-codex"
        return normalized

    @staticmethod
    def _oh_event_label(event: Any) -> str | None:
        """Extract a short, emoji-rich, laymen-friendly label from an OpenHands SDK event.

        Every possible OpenHands tool has a styled label so that each
        streamed action reads naturally for non-technical users while
        still showing the raw command/path for techie users.
        """
        import json as _json
        from openhands.sdk.event.llm_convertible.action import ActionEvent
        from openhands.sdk.event.llm_convertible.message import MessageEvent
        from openhands.sdk.event.llm_convertible.observation import ObservationEvent

        def _short_path(p: str) -> str:
            if not p:
                return ""
            parts = p.replace("\\", "/").rstrip("/").split("/")
            keep = [x for x in parts if x and x != "."]
            if len(keep) <= 3:
                return "/".join(keep)
            return "…/" + "/".join(keep[-2:])

        def _short_cmd(cmd: str) -> str:
            if not cmd:
                return ""
            if cmd.startswith("cd ") and "&&" in cmd:
                cmd = cmd.split("&&", 1)[1].strip()
            if len(cmd) > 80:
                return cmd[:77] + "…"
            return cmd

        def _parse_args(ev: ActionEvent) -> dict:
            action = ev.action
            if action is not None:
                # Pull all common attributes for maximum coverage
                d: dict[str, str] = {}
                for attr in ("command", "path", "pattern", "query", "url",
                              "text", "content", "element_index", "direction",
                              "tab_id", "key", "value", "message", "task"):
                    v = getattr(action, attr, None)
                    if v is not None:
                        d[attr] = str(v)
                return d
            try:
                raw = ev.tool_call.arguments
                if isinstance(raw, str):
                    return _json.loads(raw)
                if isinstance(raw, dict):
                    return raw
            except Exception:
                pass
            return {}

        # ── per-tool labellers ──────────────────────────────────────

        def _label_terminal(a: dict) -> str | None:
            cmd = (a.get("command") or "").strip()
            return f"🖥️ Running `{_short_cmd(cmd)}`" if cmd else None

        def _label_file_editor(a: dict) -> str:
            cmd = (a.get("command") or "").strip()
            path = _short_path(a.get("path") or "")
            if cmd == "view":
                return f"👀 Reading `{path}`" if path else "👀 Reading a file"
            if cmd == "create":
                return f"📝 Creating `{path}`" if path else "📝 Creating a new file"
            if cmd in ("str_replace", "insert"):
                return f"✏️ Editing `{path}`" if path else "✏️ Editing a file"
            if cmd == "undo_edit":
                return f"↩️ Undoing changes to `{path}`" if path else "↩️ Undoing an edit"
            return f"📄 Checking `{path}`" if path else "📄 Working with a file"

        def _label_glob(a: dict) -> str:
            pat = (a.get("pattern") or "").strip()
            return f"🔍 Searching for files matching `{pat}`" if pat else "🔍 Searching for files"

        def _label_grep(a: dict) -> str:
            q = (a.get("query") or a.get("pattern") or "").strip()
            return f"🔎 Searching file contents for `{q}`" if q else "🔎 Searching file contents"

        def _label_apply_patch(a: dict) -> str:
            path = _short_path(a.get("path") or "")
            return f"🩹 Applying a code patch to `{path}`" if path else "🩹 Applying a code patch"

        def _label_delegate(a: dict) -> str:
            task = (a.get("task") or a.get("message") or "").strip()
            if task:
                preview = task[:60] + "…" if len(task) > 60 else task
                return f"🤝 Delegating: {preview}"
            return "🤝 Delegating to a sub-agent"

        def _label_task_tracker(a: dict) -> str:
            return "📋 Updating task progress"

        # Browser tools
        def _label_navigate(a: dict) -> str:
            url = (a.get("url") or "").strip()
            if url:
                short = url[:60] + "…" if len(url) > 60 else url
                return f"🌐 Opening `{short}`"
            return "🌐 Opening a webpage"

        def _label_click(a: dict) -> str:
            idx = a.get("element_index") or ""
            return f"👆 Clicking element #{idx}" if idx else "👆 Clicking on the page"

        def _label_type(a: dict) -> str:
            text = (a.get("text") or a.get("content") or "").strip()
            if text:
                preview = text[:40] + "…" if len(text) > 40 else text
                return f"⌨️ Typing `{preview}`"
            return "⌨️ Typing into the page"

        def _label_scroll(a: dict) -> str:
            d = (a.get("direction") or "").strip().lower()
            if d == "up":
                return "📜 Scrolling up"
            if d == "down":
                return "📜 Scrolling down"
            return "📜 Scrolling the page"

        def _label_get_state(_a: dict) -> str:
            return "👁️ Reading the page layout"

        def _label_get_content(_a: dict) -> str:
            return "👀 Reading page content"

        def _label_go_back(_a: dict) -> str:
            return "⬅️ Going back to previous page"

        def _label_list_tabs(_a: dict) -> str:
            return "🗂️ Listing open browser tabs"

        def _label_switch_tab(a: dict) -> str:
            tid = a.get("tab_id") or ""
            return f"🔀 Switching to tab {tid}" if tid else "🔀 Switching browser tab"

        def _label_close_tab(a: dict) -> str:
            tid = a.get("tab_id") or ""
            return f"❌ Closing tab {tid}" if tid else "❌ Closing a browser tab"

        def _label_storage(a: dict, get: bool = True) -> str:
            key = (a.get("key") or "").strip()
            if get:
                return f"💾 Reading stored value `{key}`" if key else "💾 Reading stored data"
            return f"💾 Saving value for `{key}`" if key else "💾 Saving data"

        # Gemini-specific file tools
        def _label_read_file(a: dict) -> str:
            path = _short_path(a.get("path") or "")
            return f"👀 Reading `{path}`" if path else "👀 Reading a file"

        def _label_write_file(a: dict) -> str:
            path = _short_path(a.get("path") or "")
            return f"📝 Writing `{path}`" if path else "📝 Writing a file"

        def _label_edit(a: dict) -> str:
            path = _short_path(a.get("path") or "")
            return f"✏️ Editing `{path}`" if path else "✏️ Editing a file"

        def _label_list_directory(a: dict) -> str:
            path = _short_path(a.get("path") or "")
            return f"📂 Listing `{path}`" if path else "📂 Listing directory contents"

        # ── dispatch table ──────────────────────────────────────────

        _TOOL_LABELS = {
            "terminal": _label_terminal,
            "file_editor": _label_file_editor,
            "planning_file_editor": _label_file_editor,
            "glob": _label_glob,
            "grep": _label_grep,
            "apply_patch": _label_apply_patch,
            "delegate": _label_delegate,
            "task_tracker": _label_task_tracker,
            # Browser tools
            "navigate": _label_navigate,
            "click": _label_click,
            "type": _label_type,
            "scroll": _label_scroll,
            "get_state": _label_get_state,
            "get_content": _label_get_content,
            "go_back": _label_go_back,
            "list_tabs": _label_list_tabs,
            "switch_tab": _label_switch_tab,
            "close_tab": _label_close_tab,
            "get_storage": lambda a: _label_storage(a, get=True),
            "set_storage": lambda a: _label_storage(a, get=False),
            # Consultation / reasoning tools
            "tom_consult": lambda _a: "🧠 Consulting an expert",
            "sleeptime_compute": lambda _a: "🧠 Running a deep analysis",
            # Gemini-specific file tools
            "read_file": _label_read_file,
            "write_file": _label_write_file,
            "edit": _label_edit,
            "list_directory": _label_list_directory,
        }

        if isinstance(event, ActionEvent):
            tool = event.tool_name or "action"

            if tool == "finish":
                return "✅ Wrapping up"

            labeller = _TOOL_LABELS.get(tool)
            if labeller:
                result = labeller(_parse_args(event))
                if result:
                    return result
                # labeller returned None/empty — fall through to generic
            # Fallback for unknown tools — use summary if available
            summary = event.summary or ""
            if summary:
                return f"🔧 {summary[:100]}"
            return f"🔧 Using {tool}"

        if isinstance(event, ObservationEvent):
            return None

        if isinstance(event, MessageEvent) and event.source == "agent":
            return "💬 Thinking through the approach"

        return None

    @staticmethod
    def _oh_event_modified_path(event: Any) -> str | None:
        """Return the file path if this event is a file-mutating action, else None."""
        import json as _json
        from openhands.sdk.event.llm_convertible.action import ActionEvent

        if not isinstance(event, ActionEvent):
            return None

        tool = event.tool_name or ""
        _MUTATING_TOOLS = {"write_file", "edit", "apply_patch"}
        _EDITOR_TOOLS = {"file_editor", "planning_file_editor"}
        _EDITOR_MUT_CMDS = {"create", "str_replace", "insert"}

        action = event.action
        path = getattr(action, "path", None) or ""
        command = getattr(action, "command", None) or ""

        if not path:
            try:
                raw = event.tool_call.arguments
                args = _json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
                path = args.get("path", "")
                command = command or args.get("command", "")
            except Exception:
                return None

        if not path:
            return None

        if tool in _MUTATING_TOOLS:
            return path
        if tool in _EDITOR_TOOLS and command in _EDITOR_MUT_CMDS:
            return path

        return None

    @staticmethod
    def _oh_extract_final_message(conversation: Any) -> str:
        """Walk conversation events backwards to find the last agent message."""
        from openhands.sdk.event.llm_convertible.action import ActionEvent
        from openhands.sdk.event.llm_convertible.message import MessageEvent
        from openhands.sdk.llm.message import TextContent

        events = conversation.state.events
        for i in range(len(events) - 1, -1, -1):
            ev = events[i]
            if isinstance(ev, ActionEvent) and ev.tool_name == "finish":
                action = ev.action
                msg = getattr(action, "message", None)
                if isinstance(msg, str) and msg.strip():
                    return msg.strip()
                summary = ev.summary
                if isinstance(summary, str) and summary.strip():
                    return summary.strip()
            if isinstance(ev, MessageEvent) and ev.source == "agent":
                parts: list[str] = []
                for content in ev.llm_message.content:
                    if isinstance(content, TextContent):
                        parts.append(content.text)
                text = "\n".join(parts).strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _result_is_unusable(text: str) -> bool:
        """Check if a final result is unsuitable for user delivery."""
        raw = text or ""
        t = " ".join(raw.split()).strip()
        if not t:
            return True
        if len(t.split()) < 6:
            return True
        if any(tok in t.lower() for tok in ["tool call", "tool_calls", "system prompt", "developer message"]):
            return True
        lower = t.lower()
        if lower.startswith("error calling llm:") or "badrequest" in lower.replace(" ", ""):
            return True
        if any(phrase in lower for phrase in [
            "would you like me to", "shall i ", "do you want me to",
            "want me to proceed", "like me to proceed", "like me to apply", "like me to fix",
        ]):
            return True
        if re.search(r'^#{1,3}\s', raw, re.MULTILINE) and raw.count('\n') > 3:
            return True
        if re.search(r'\[[a-zA-Z][\w\s]{4,}\]', t):
            return True
        if len(t) > 2000:
            return True
        return False

    @staticmethod
    def _status_rewrite_is_unusable(text: str) -> bool:
        """Looser sanity check for rewritten status summaries."""
        raw = (text or "").strip()
        if not raw:
            return True
        if len(raw.split()) < 4:
            return True
        lower = raw.lower()
        if lower.startswith("error calling llm:") or "badrequest" in lower.replace(" ", ""):
            return True
        if any(tok in lower for tok in ["tool call", "tool_calls", "system prompt", "developer message"]):
            return True
        if "|" in raw:
            return True
        if re.search(
            r"(?im)^(task|reference|status|created|started|elapsed|step|current action|execution phases|recent command/activity trace|outcome|result|failure signal):",
            raw,
        ):
            return True
        if re.search(
            r"(?i)\b(step|elapsed|latest action|current action|execution phases|recent command/activity trace|failure signal|result|outcome)\s*:",
            raw,
        ):
            return True
        return False

    @staticmethod
    def _looks_like_telemetry_block(text: str) -> bool:
        """Detect if text is likely the raw telemetry template."""
        if not text:
            return False
        lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
        if not lines:
            return False
        telemetry_markers = [
            "task:", "reference:", "status:", "created:", "started:",
            "elapsed:", "current action:", "execution phases:", "step:",
            "recent command/activity trace:", "failure signal:", "outcome:",
        ]
        marker_hits = sum(1 for line in lines for m in telemetry_markers if line.startswith(m))
        if marker_hits >= 3:
            return True
        return False

    def _format_status_fallback(self, status_payload: str | None) -> str:
        """Build a concise natural-language fallback status message."""
        if not status_payload:
            return "No status details are available yet."

        payload = status_payload.strip()
        if not payload:
            return "No status details are available yet."

        # Parse telemetry task blocks if present.
        task_blocks: list[str] = []
        current: list[str] = []
        for raw_line in payload.splitlines():
            line = raw_line.rstrip()
            if line.startswith("Task:"):
                if current:
                    task_blocks.append("\n".join(current))
                current = [line]
                continue
            if current:
                current.append(line)
        if current:
            task_blocks.append("\n".join(current))

        def _clean(value: str | None, limit: int = 800) -> str:
            text = " ".join((value or "").split()).strip()
            if not text:
                return ""
            if len(text) <= limit:
                return text
            clipped = text[:limit]
            if " " in clipped:
                clipped = clipped.rsplit(" ", 1)[0]
            return clipped + "..."

        def _parse_block(block: str) -> dict[str, str]:
            parsed: dict[str, str] = {}
            for raw_line in block.splitlines():
                if ":" not in raw_line:
                    continue
                key, value = raw_line.split(":", 1)
                key = key.strip().lower()
                value = value.strip()
                if key and value:
                    parsed[key] = value
            return parsed

        def _describe(parsed: dict[str, str]) -> str:
            task_name = _clean(parsed.get("task"), limit=140) or "This task"
            state = (parsed.get("status") or "running").lower()
            action = _clean(parsed.get("current action"), limit=240)
            elapsed = _clean(parsed.get("elapsed"), limit=80)
            outcome = _clean(parsed.get("outcome"), limit=1100)
            failure = _clean(parsed.get("failure signal"), limit=500)

            if "completed" in state:
                sentence = f"{task_name} is complete."
                if outcome:
                    sentence = f"{sentence} {outcome}"
                return sentence.strip()
            if "failed" in state:
                sentence = f"{task_name} failed."
                if failure:
                    sentence = f"{sentence} {failure}"
                return sentence.strip()
            if "cancelled" in state:
                return f"{task_name} was cancelled."
            if "queued" in state:
                return f"{task_name} is queued and waiting to start."

            sentence = f"{task_name} is still running."
            if action and "thinking through the approach" not in action.lower():
                sentence = f"{sentence} Last action: {action}."
            if elapsed:
                sentence = f"{sentence} Runtime so far: {elapsed}."
            return sentence.strip()

        if task_blocks:
            rendered = [_describe(_parse_block(block)) for block in task_blocks]
            rendered = [item for item in rendered if item]
            if rendered:
                return " ".join(rendered)

        # Non-telemetry payload fallback: keep it concise and readable.
        plain = _clean(payload, limit=1200)
        return plain if plain else "No status details are available yet."

    @staticmethod
    def _sanitize_status_narrative(text: str) -> str:
        """Strip telemetry-like artifacts from status prose."""
        if not text:
            return ""
        cleaned = text.replace("|", " ")
        cleaned = re.sub(
            r"(?i)\b(task|reference|status|created|started|elapsed|step|current action|latest action|execution phases|recent command/activity trace|failure signal|result|outcome)\s*:\s*",
            "",
            cleaned,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    async def _rewrite_result(self, task: Task, raw_result: str) -> str:
        """Rewrite an unusable result into a concise user-facing message."""
        recent_actions = "\n".join(f"- {a}" for a in (task.actions_completed[-12:] or []))
        if not recent_actions:
            recent_actions = "- (none yet)"
        evidence = raw_result[:1500] if raw_result else "(no output)"
        system = (
            f"{self.persona_prompt}\n\n"
            "You are rewriting a task result into a concise status update for the user.\n"
            "Stay in character.\n"
            "RULES:\n"
            "- If the output contains errors, tracebacks, or non-zero exit codes, the task FAILED. Say so.\n"
            "- Do NOT fabricate success.\n"
            "- Do NOT include API keys, tokens, passwords, or credentials.\n"
            "- Do NOT paste raw file contents, source code, tracebacks, or command output.\n"
            "- Do NOT use markdown headers (##) or structured formatting.\n"
            "- Do NOT ask the user for permission.\n"
            "- Keep it under 800 characters. Talk naturally."
        )
        user_msg = (
            f"Task: {task.label}\n"
            f"Request: {task.description}\n\n"
            f"Actions taken:\n{recent_actions}\n\n"
            f"Raw output (truncated):\n{evidence}\n\n"
            "Rewrite this into a brief, natural status update."
        )
        try:
            r = await self._provider_chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                tools=None, model=self.model, max_tokens=600, session_key=None,
            )
            out = (getattr(r, "content", None) or "").strip()
            if out and not self._result_is_unusable(out):
                return out
        except Exception as e:
            logger.warning(f"Task [{task.id}] result rewrite failed: {e}")
        return raw_result

    async def _rewrite_status_report(self, status_payload: str | None, user_query: str | None = None) -> str | None:
        """Rewrite raw status telemetry into an in-character, intelligent status update."""
        if not status_payload:
            return None

        query = (user_query or "status").strip() or "status"
        system = (
            f"{self.persona_prompt}\n\n"
            "You are analyzing task status and replying to the user in-character.\n"
            "Use only the provided status digest as evidence.\n"
            "Do not invent details that are not present.\n"
            "For running tasks, use this structure in plain prose: just finished X, now working on Y, left to go Z.\n"
            "For completed tasks, state completion and concise outcome.\n"
            "For failed tasks, state failure and the blocker.\n"
            "Keep it concise, natural, and conversational, all in character voice.\n"
            "Never output key/value status fields like Task:, Step:, Elapsed:, Result:, or Progress:.\n"
            "Never use pipe separators.\n"
            "Output plain narrative prose only."
        )
        user_msg = (
            f"User query: {query}\n\n"
            f"Status digest:\n{status_payload}\n\n"
            "Rewrite this as one clear status reply."
        )
        try:
            r = await self._provider_chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                tools=None,
                model=self.model,
                max_tokens=600,
                session_key=None,
            )
            out = self._sanitize_status_narrative((getattr(r, "content", None) or "").strip())
            if out and not self._status_rewrite_is_unusable(out) and not self._looks_like_telemetry_block(out):
                return out
        except Exception as e:
            logger.warning(f"Status rewrite failed: {e}")
        return self._format_status_fallback(status_payload)

    def _get_workspace_index_block(self) -> str:
        """Get workspace index for injection into task prompts."""
        _MAX_CHARS = 5000
        try:
            index = self.workspace_index.get()
            if index:
                if len(index) > _MAX_CHARS:
                    index = index[: _MAX_CHARS - 1].rstrip() + "…"
                return (
                    "## Workspace File Map\n"
                    "You already know the workspace layout. Skip list_dir unless you need "
                    "a subdirectory not shown here. Jump straight to the work.\n\n"
                    f"{index}"
                )
        except Exception as e:
            logger.debug(f"Failed to get workspace index: {e}")
        return ""

    @staticmethod
    def _status_action_intent(action: str) -> str:
        """Infer a high-level intention from an action label."""
        raw = (action or "").lower()
        if "🖥️" in raw or "terminal" in raw or "running `" in raw:
            return "shell command execution"
        if "📖" in raw or "searching for files" in raw or "reading" in raw:
            return "file discovery"
        if "✏️" in raw or "📝" in raw or "editing" in raw:
            return "file edits"
        if "🔍" in raw or "searching" in raw:
            return "code search"
        if "🌐" in raw or "opening" in raw or "clicking" in raw:
            return "web browsing"
        if "📋" in raw or "⏳" in raw or "delegat" in raw:
            return "coordination"
        return "task progress"

    @staticmethod
    def _format_command_trace(actions: list[str], limit: int = 6) -> str:
        """Format recent action labels as a readable execution trace."""
        if not actions:
            return "No commands/actions recorded yet."
        tail = list(actions[-limit:])
        parts: list[str] = []
        start = max(1, len(actions) - limit + 1)
        for idx, action in enumerate(tail, start=start):
            if not action:
                continue
            parts.append(f"{idx:>3}. {action}")
        return "\n".join(parts) if parts else "No commands/actions recorded yet."

    def _build_status_report(self, task: Task) -> str:
        """Build an intelligent status summary from task internals."""
        ref = task.completion_reference or task.reference
        created = task.created_at.isoformat(timespec="seconds").replace("T", " ")
        started = (
            task.started_at.isoformat(timespec="seconds").replace("T", " ")
            if task.started_at else "not started"
        )
        finished = (
            task.completed_at.isoformat(timespec="seconds").replace("T", " ")
            if task.completed_at else None
        )

        if task.status == TaskStatus.RUNNING:
            state = "actively running"
        elif task.status == TaskStatus.COMPLETED:
            state = "completed"
        elif task.status == TaskStatus.FAILED:
            state = "failed"
        elif task.status == TaskStatus.CANCELLED:
            state = "cancelled"
        else:
            state = "queued"

        if finished is not None:
            duration_src = task.completed_at or datetime.now()
        elif task.started_at:
            duration_src = datetime.now()
        else:
            duration_src = task.started_at or task.created_at
        elapsed = int((duration_src - (task.started_at or task.created_at)).total_seconds())
        mins, secs = divmod(max(elapsed, 0), 60)
        elapsed_text = f"{mins}m {secs}s" if mins else f"{secs}s"

        latest_action = task.current_action or (task.actions_completed[-1] if task.actions_completed else "No action observed yet")
        recent_trace = self._format_command_trace(task.actions_completed, limit=8)
        if task.actions_completed:
            phases = ", ".join(
                sorted(
                    {self._status_action_intent(item) for item in task.actions_completed[-12:] if item},
                )
            )
        else:
            phases = "file bootstrap"

        lines = [
            f"Task: {task.label}",
            f"Reference: {ref}",
            f"Status: {state} ({task.status.value})",
            f"Created: {created}",
            f"Started: {started}",
            f"Elapsed: {elapsed_text}",
            f"Step: {task.iteration}",
        ]

        if task.current_action:
            lines.append(f"Current action: {latest_action}")
        else:
            lines.append(f"Current action: {latest_action}")

        lines.append(f"Execution phases: {phases}")
        lines.append("Recent command/activity trace:")
        lines.append(recent_trace)

        if task.status == TaskStatus.FAILED and task.error:
            lines.append(f"Failure signal: {task.error}")
        if task.status == TaskStatus.COMPLETED and task.result:
            snippet = " ".join(task.result.split()).strip()
            if len(snippet) > 1200:
                snippet = snippet[:1200]
                if " " in snippet:
                    snippet = snippet.rsplit(" ", 1)[0]
                snippet += "..."
            lines.append(f"Outcome: {snippet}")

        if task.status == TaskStatus.QUEUED:
            lines.append("Next: task is waiting for execution resources.")

        return "\n".join(lines)

    @staticmethod
    def _clip_status_text(value: str | None, limit: int = 240) -> str:
        text = " ".join((value or "").split()).strip()
        if not text:
            return ""
        if len(text) <= limit:
            return text
        clipped = text[:limit]
        if " " in clipped:
            clipped = clipped.rsplit(" ", 1)[0]
        return clipped + "..."

    def _status_left_to_go(self, task: Task) -> str:
        if task.status == TaskStatus.COMPLETED:
            return "nothing left; this one is done"
        if task.status == TaskStatus.FAILED:
            return "recover from the failure and rerun the remaining checks"
        if task.status == TaskStatus.CANCELLED:
            return "nothing left; task was cancelled"
        if task.status == TaskStatus.QUEUED:
            return "start execution as soon as resources are available"
        return "finish the remaining execution steps and post the final result"

    def _status_just_finished(self, task: Task) -> str:
        actions = [a for a in (task.actions_completed or []) if a]
        if not actions:
            if task.status == TaskStatus.COMPLETED:
                return "the full task execution"
            return "initial setup"
        if task.current_action and len(actions) >= 2 and actions[-1] == task.current_action:
            return actions[-2]
        return actions[-1]

    def _build_status_digest_from_tasks(self, tasks: list[Task]) -> str:
        """Build a compact, structured digest for status narration."""
        lines: list[str] = []
        for task in tasks[:4]:
            state = task.status.value
            just_finished = self._clip_status_text(self._status_just_finished(task), limit=220)
            working_now = self._clip_status_text(task.current_action or "", limit=220)
            left_to_go = self._clip_status_text(self._status_left_to_go(task), limit=220)
            outcome = self._clip_status_text(task.result or "", limit=700)
            failure = self._clip_status_text(task.error or "", limit=400)

            line = (
                f"Task '{task.label}' is {state}. "
                f"Just finished: {just_finished or 'n/a'}. "
                f"Working now: {working_now or 'n/a'}. "
                f"Left to go: {left_to_go}."
            )
            if outcome and task.status == TaskStatus.COMPLETED:
                line += f" Outcome: {outcome}"
            if failure and task.status == TaskStatus.FAILED:
                line += f" Failure: {failure}"
            lines.append(line.strip())
        if not lines:
            return "No tasks to report on."
        return "\n".join(lines)

    def _status_fallback_from_tasks(self, tasks: list[Task]) -> str:
        """Deterministic narrative fallback for status requests."""
        if not tasks:
            return "No tasks to report on."
        sentences: list[str] = []
        for task in tasks[:4]:
            just_finished = self._clip_status_text(self._status_just_finished(task), limit=180) or "initial setup"
            working_now = self._clip_status_text(task.current_action or "", limit=180)
            left_to_go = self._clip_status_text(self._status_left_to_go(task), limit=180)
            if task.status == TaskStatus.RUNNING:
                if working_now:
                    sentences.append(
                        f"{task.label}: just finished {just_finished}; working on {working_now}; left to go is {left_to_go}."
                    )
                else:
                    sentences.append(
                        f"{task.label}: just finished {just_finished}; left to go is {left_to_go}."
                    )
            elif task.status == TaskStatus.COMPLETED:
                outcome = self._clip_status_text(task.result or "", limit=500)
                if outcome:
                    sentences.append(f"{task.label}: complete. {outcome}")
                else:
                    sentences.append(f"{task.label}: complete.")
            elif task.status == TaskStatus.FAILED:
                failure = self._clip_status_text(task.error or "", limit=280)
                if failure:
                    sentences.append(f"{task.label}: failed. {failure}")
                else:
                    sentences.append(f"{task.label}: failed.")
            elif task.status == TaskStatus.QUEUED:
                sentences.append(f"{task.label}: queued; left to go is {left_to_go}.")
            else:
                sentences.append(f"{task.label}: cancelled.")
        return " ".join(sentences)

    def _build_status_summary(self, task_ref: str | None) -> str:
        """Build a status snapshot across tasks."""
        if task_ref:
            task = self.registry.get_by_ref(task_ref)
            if not task:
                return (
                    f"No task found for '{task_ref}'.\n"
                    "No ref found often means the task is still queued in another process "
                    "or it was already cleared from active state."
                )
            return self._build_status_report(task)

        active = self.registry.get_active_tasks()
        recent = self.registry.get_recent_completed(5)
        if not active and not recent:
            return "No active or recently completed tasks to report."

        lines = []
        if active:
            lines.append("Active tasks:")
            for task in active:
                lines.append("")
                lines.append(self._build_status_report(task))
        if recent:
            lines.append("")
            lines.append("Recent completed tasks:")
            for task in recent:
                lines.append("")
                lines.append(self._build_status_report(task))
        return "\n".join(lines).strip()

    def _build_task_prompt(self, task: Task, conversation_context: str | None = None) -> str:
        """Build the task prompt passed to the OpenHands agent."""
        from kyber.utils.helpers import current_datetime_str

        workspace_index = self._get_workspace_index_block()
        workspace_index_text = workspace_index if workspace_index else "(workspace index unavailable)"
        directives_block = self._build_worker_workspace_directives_block()
        context_block = ""
        if conversation_context:
            context_block = (
                "Conversation history (recent messages between the user and assistant):\n"
                "Use this context to understand what was discussed, what files were "
                "worked on, and what the user is referring to.\n\n"
                f"{conversation_context}\n\n"
            )

        return (
            f"{self.persona_prompt}\n\n"
            "You are executing a background task for the user.\n"
            f"Current time: {current_datetime_str(self.timezone)}\n"
            f"Workspace: {self.workspace}\n\n"
            "Execution requirements:\n"
            "- Perform the requested work directly (edit files, run commands, verify outcomes).\n"
            "- Do not ask for permission; proceed autonomously.\n"
            "- If a command fails, diagnose and fix, then retry when appropriate.\n"
            "- If a command appears stuck, switch approaches immediately instead of waiting indefinitely.\n"
            "- Prefer bounded/timeout command variants for long-running operations.\n"
            "- If external instructions/docs are required, fetch/read them and execute exactly against those instructions.\n"
            "- Validate assumptions; do not guess when evidence can be gathered.\n"
            "- Do not declare success until acceptance criteria are actually met.\n"
            "- Keep updates concise and factual.\n"
            "- Do not leak secrets (API keys, tokens, passwords).\n"
            "- Final response must be a concise, characterful, natural-language status update (<1200 chars).\n"
            "- Keep the persona and tone from the persona prompt in all task narration.\n"
            "- Do not dump raw file contents, tracebacks, or large command output.\n\n"
            f"{context_block}"
            f"{directives_block}\n\n"
            f"{workspace_index_text}\n\n"
            f"Task:\n{task.description}\n"
        )

    async def _run_openhands_task(
        self,
        task: Task,
        conversation_context: str | None = None,
        *,
        publish_completion: bool = True,
        session_key: str | None = None,
        project_key: str | None = None,
    ) -> None:
        """Execute a task with the OpenHands agent and optionally queue completion delivery."""
        from kyber.utils.helpers import redact_secrets

        namespace_session_key = session_key or self._session_key(task.origin_channel, task.origin_chat_id)
        namespace = self._openhands_namespace(namespace_session_key, project_key)

        lock = self._get_or_create_openhands_namespace_lock(
            self._openhands_conversation_locks,
            namespace,
        )

        try:
            self.registry.mark_started(task.id)
            logger.info(f"Task started: {task.label} [{task.id}]")

            async with lock:
                execute_fn = self._execute_openhands
                execute_sig = inspect.signature(execute_fn)
                result = ""
                last_error: Exception | None = None
                max_recovery_attempts = 4
                force_next_retry_fresh = False
                for attempt in range(max_recovery_attempts):
                    retry_hint = None
                    force_fresh = force_next_retry_fresh or (attempt > 0)
                    if attempt > 0:
                        last_action = ""
                        for action in reversed(task.actions_completed or []):
                            if action and action != task.current_action:
                                last_action = action
                                break
                        if not last_action:
                            last_action = task.current_action or "the previous blocked step"
                        retry_hint = (
                            "Previous attempt stalled. Continue the same task and finish it. "
                            "Do not repeat the same blocking command sequence. "
                            "Use a different approach immediately, prefer bounded/timeout commands, "
                            "and verify progress after each step. "
                            f"Avoid re-running this blocked step as-is: {last_action}"
                        )
                        self.registry.update_progress(
                            task.id,
                            current_action=f"Recovery attempt {attempt + 1}/{max_recovery_attempts} after stall",
                            action_completed="Detected stalled run; switching to a different approach",
                        )
                    execute_kwargs = {
                        "conversation_context": conversation_context,
                        "session_key": namespace_session_key,
                        "project_key": project_key,
                        "retry_hint": retry_hint,
                        "force_fresh_conversation": force_fresh,
                    }
                    filtered_kwargs = {
                        k: v
                        for k, v in execute_kwargs.items()
                        if k in execute_sig.parameters
                    }
                    try:
                        result = await execute_fn(task, **filtered_kwargs)
                        break
                    except Exception as run_error:
                        last_error = run_error
                        if (
                            attempt < (max_recovery_attempts - 1)
                            and self._is_retryable_openhands_error(run_error)
                        ):
                            if Orchestrator._requires_fresh_openhands_conversation(run_error):
                                force_next_retry_fresh = True
                            logger.warning(
                                f"Task [{task.id}] run stalled; retrying with fresh conversation (attempt {attempt + 2}/{max_recovery_attempts}): {run_error}",
                            )
                            continue
                        raise
                if not result and last_error is not None:
                    raise last_error
            result = redact_secrets(result)
            if self._result_is_unusable(result):
                logger.info(f"Task [{task.id}] result unusable, rewriting")
                result = await self._rewrite_result(task, result)
                result = redact_secrets(result)

            self.registry.mark_completed(task.id, result)
            logger.info(f"Task completed: {task.label} [{task.id}]")

        except asyncio.CancelledError:
            self.registry.mark_cancelled(task.id, "cancelled")
            logger.info(f"Task cancelled: {task.label} [{task.id}]")
        except Exception as e:
            self.registry.mark_failed(task.id, str(e))
            logger.error(f"Task failed: {task.label} [{task.id}] - {e}")
        finally:
            if self.narrator:
                try:
                    await self.narrator.flush_and_unregister(task.id)
                except Exception as e:
                    logger.warning(f"Task [{task.id}] narration flush failed: {e}")
            if publish_completion:
                await self.completion_queue.put(task)

    async def _execute_openhands(
        self,
        task: Task,
        conversation_context: str | None = None,
        *,
        session_key: str | None = None,
        project_key: str | None = None,
        retry_hint: str | None = None,
        force_fresh_conversation: bool = False,
    ) -> str:
        """Run the OpenHands SDK conversation for a single task."""
        ensure_openhands_runtime_dirs()
        from openhands.sdk import LLM, Agent, Tool
        from openhands.sdk.event.base import Event
        from openhands.tools.terminal import TerminalTool
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.tom_consult import TomConsultTool, SleeptimeComputeTool

        ws = self.workspace.resolve()
        prov_name = getattr(self.provider, "provider_name", None)
        is_custom = getattr(self.provider, "is_custom", False)
        llm_model = self._resolve_model_string(
            self.model,
            prov_name,
            is_custom,
            self.provider.api_base,
        )
        api_base = self.provider.api_base

        llm: Any | None = None
        provider_lower = (prov_name or "").strip().lower()
        requested_subscription_model = self._normalize_openai_model_name(llm_model)
        subscription_model = self._effective_subscription_model(llm_model)
        use_subscription = (
            provider_lower == "openai"
            and self._is_openai_subscription_model(llm_model)
        )
        if use_subscription:
            try:
                if subscription_model != requested_subscription_model:
                    logger.warning(
                        "OpenAI subscription model '%s' is currently unstable via "
                        "OpenHands; falling back to '%s' for task execution.",
                        requested_subscription_model,
                        subscription_model,
                    )
                llm = await asyncio.to_thread(
                    LLM.subscription_login,
                    vendor="openai",
                    model=subscription_model,
                    open_browser=False,
                    skip_consent=True,
                )
                logger.info(
                    f"Using OpenAI subscription auth for OpenHands model '{subscription_model}'"
                )
            except Exception as e:
                if self.provider.api_key:
                    logger.warning(
                        "OpenAI subscription auth failed; falling back to API key auth: "
                        f"{e}"
                    )
                else:
                    raise RuntimeError(
                        "OpenAI subscription auth failed. "
                        "Complete OpenHands login for your ChatGPT Plus/Pro account "
                        f"or set an OpenAI API key. ({e})"
                    ) from e

        if llm is None:
            llm_kwargs: dict[str, Any] = {
                "model": llm_model,
                "api_key": self.provider.api_key or "",
                "temperature": 0.2,
            }
            if api_base:
                llm_kwargs["base_url"] = api_base
            llm = LLM(**llm_kwargs)

        tools = [Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)]
        tom_params: dict[str, Any] = {
            "enable_rag": True,
            "llm_model": llm_model,
            "api_key": self.provider.api_key,
        }
        if api_base:
            tom_params["api_base"] = api_base
        if not self.provider.api_key:
            logger.warning(
                "No API key configured for TOM tools. Registering tools in "
                "deferred mode to keep OpenHands conversations resumable."
            )
        tools.extend(
            [
                Tool(name=TomConsultTool.name, params=tom_params),
                Tool(name=SleeptimeComputeTool.name, params=tom_params),
            ]
        )
        agent = Agent(llm=llm, tools=tools)

        # Progress callback bridge — capture event loop before entering bg thread
        _loop = asyncio.get_running_loop()
        progress_step = max(1, int(task.iteration))
        modified_files: list[str] = []
        stuck_detected = {"value": False}
        last_progress_at = {"value": time.monotonic()}

        def _state_update_callback(event: Event) -> None:
            try:
                last_progress_at["value"] = time.monotonic()
                if getattr(event, "key", None) == "execution_status":
                    stuck_detected["value"] = stuck_detected["value"] or self._status_is_stuck(
                        getattr(event, "value", None)
                    )
            except Exception:
                logger.debug("OpenHands state callback error", exc_info=True)

        def _event_callback(event: Event) -> None:
            nonlocal progress_step
            try:
                # Track files modified by the agent
                mod_path = self._oh_event_modified_path(event)
                if mod_path and mod_path not in modified_files:
                    modified_files.append(mod_path)

                label = self._oh_event_label(event)
                if not label:
                    return

                last_progress_at["value"] = time.monotonic()
                progress_step += 1
                self.registry.update_progress(
                    task.id, iteration=progress_step, current_action=label, action_completed=label,
                )
                if self.narrator:
                    result = self.narrator.narrate(task.id, label)
                    if asyncio.iscoroutine(result):
                        asyncio.run_coroutine_threadsafe(result, _loop)
            except RuntimeError:
                raise
            except Exception:
                logger.debug("OpenHands event callback error", exc_info=True)

        task_prompt = self._build_task_prompt(task, conversation_context)
        if retry_hint:
            task_prompt = (
                f"{task_prompt}\n\n"
                f"Recovery directive:\n{retry_hint}\n"
            )

        namespace = self._openhands_namespace(
            session_key or self._session_key(task.origin_channel, task.origin_chat_id),
            project_key,
        )
        if force_fresh_conversation:
            self._reset_openhands_namespace_conversation(
                namespace,
                clear_persisted_state=True,
            )
        conversation = self._build_or_get_openhands_conversation(
            namespace=namespace,
            agent=agent,
            event_callback=_event_callback,
            state_callback=_state_update_callback,
        )

        async def _abort_conversation() -> None:
            for method_name in ("cancel", "interrupt", "stop"):
                method = getattr(conversation, method_name, None)
                if callable(method):
                    try:
                        await asyncio.to_thread(method)
                        return
                    except Exception:
                        continue

        conversation.send_message(task_prompt)
        idle_timeout_seconds = max(120, int(self.exec_timeout) * 3)
        hard_timeout_seconds = max(900, idle_timeout_seconds * 3)
        start_time = time.monotonic()
        run_task = asyncio.create_task(asyncio.to_thread(conversation.run))
        while not run_task.done():
            await asyncio.sleep(2.0)
            if run_task.done():
                break
            now = time.monotonic()
            idle_for = now - last_progress_at["value"]
            total = now - start_time
            if stuck_detected["value"]:
                await _abort_conversation()
                self._reset_openhands_namespace_conversation(
                    namespace,
                    clear_persisted_state=True,
                )
                run_task.cancel()
                raise RuntimeError(
                    "OpenHands reported a stuck execution state and was reset."
                )
            if idle_for >= idle_timeout_seconds:
                await _abort_conversation()
                self._reset_openhands_namespace_conversation(
                    namespace,
                    clear_persisted_state=True,
                )
                run_task.cancel()
                raise RuntimeError(
                    f"OpenHands made no progress for {int(idle_for)}s and was reset."
                )
            if total >= hard_timeout_seconds:
                await _abort_conversation()
                self._reset_openhands_namespace_conversation(
                    namespace,
                    clear_persisted_state=True,
                )
                run_task.cancel()
                raise RuntimeError(
                    f"OpenHands exceeded hard timeout ({int(total)}s) and was reset."
                )
        await run_task
        result_text = self._oh_extract_final_message(conversation)
        if (
            stuck_detected["value"]
            or self._status_is_stuck(
                getattr(conversation.state, "execution_status", None),
            )
        ):
            raise RuntimeError(
                "OpenHands stuck detector triggered. Task appears to be in an unproductive loop."
            )
        if not result_text:
            raise RuntimeError(
                "OpenHands agent completed without producing a final message."
            )
        # Store modified files for session enrichment
        self._last_modified_files[task.id] = modified_files[:20]
        return result_text

    def _spawn_task(
        self,
        task: Task,
        conversation_context: str | None = None,
        *,
        session_key: str | None = None,
        project_key: str | None = None,
    ) -> None:
        """Spawn an OpenHands task in the background."""
        if self.narrator:
            self.narrator.register_task(
                task.id, task.origin_channel, task.origin_chat_id, task.label,
            )
        async_task = asyncio.create_task(
            self._run_openhands_task(
                task,
                conversation_context,
                session_key=session_key,
                project_key=project_key,
            )
        )
        self._running_tasks[task.id] = async_task

        def _on_done(_: asyncio.Task) -> None:
            self._running_tasks.pop(task.id, None)
            if self.narrator:
                self.narrator.unregister_task(task.id)

        async_task.add_done_callback(_on_done)
        logger.info(f"Spawned task: {task.label} [{task.id}]")

    async def _run_task_inline(
        self,
        task: Task,
        conversation_context: str | None = None,
        *,
        session_key: str | None = None,
        project_key: str | None = None,
    ) -> Task:
        """Run a task to completion in the current event loop (foreground)."""
        if self.narrator:
            self.narrator.register_task(
                task.id, task.origin_channel, task.origin_chat_id, task.label,
            )
        try:
            await self._run_openhands_task(
                task,
                conversation_context,
                publish_completion=False,
                session_key=session_key,
                project_key=project_key,
            )
        finally:
            if self.narrator:
                self.narrator.unregister_task(task.id)
        return task

    def _cancel_task(self, task_id: str) -> bool:
        """Cancel a running task by task id."""
        t = self._running_tasks.get(task_id)
        if not t:
            return False
        t.cancel()
        return True

    def _spawn_security_scan(
        self,
        channel: str,
        chat_id: str,
        *,
        session_key: str | None = None,
        project_key: str | None = None,
    ) -> Task | None:
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
            self._spawn_task(
                task,
                session_key=session_key or self._session_key(channel, chat_id),
                project_key=project_key,
            )
            return task
        except Exception as e:
            logger.error(f"Failed to spawn deterministic security scan: {e}")
            return None

    async def _completion_loop(self) -> None:
        """
        Background loop that delivers task completions to the user.

        Completed tasks should deliver their actual final narration first.
        Structured telemetry rewriting is used only as a secondary fallback.
        """
        while self._running:
            try:
                task: Task | None = None

                task = await asyncio.wait_for(self.completion_queue.get(), timeout=1.0)

                raw_status: str | None = None
                try:
                    notification = ""

                    # Prefer the worker's own final response for completed tasks.
                    # This preserves the character voice and avoids telemetry-style output.
                    if task.status == TaskStatus.COMPLETED:
                        candidate = (task.result or "").strip()
                        if candidate and not self._result_is_unusable(candidate):
                            notification = candidate
                        elif candidate:
                            notification = await self._rewrite_result(task, candidate)

                    if not notification:
                        raw_status = self._build_status_report(task)
                        rewritten = await self._rewrite_status_report(
                            raw_status,
                            f"Task completion update for: {task.label}",
                        )
                        if rewritten and not self._status_rewrite_is_unusable(rewritten):
                            notification = rewritten

                    if not notification:
                        if task.status == TaskStatus.COMPLETED:
                            notification = (
                                f"{task.label} completed, but I did not get a clean final summary."
                            )
                        elif task.status == TaskStatus.FAILED:
                            error = (task.error or "unknown error").strip()
                            notification = f"{task.label} failed: {error}"
                        elif task.status == TaskStatus.CANCELLED:
                            notification = f"{task.label} was cancelled."
                        else:
                            notification = f"{task.label} finished with status: {task.status.value}."
                except Exception as e:
                    logger.error(f"Failed to build completion update for '{task.label}': {e}")
                    backup_result = (task.result or "").strip()
                    if task.status == TaskStatus.COMPLETED and backup_result:
                        notification = backup_result
                    elif task.status == TaskStatus.FAILED:
                        notification = f"{task.label} failed: {(task.error or 'unknown error').strip()}"
                    elif task.status == TaskStatus.CANCELLED:
                        notification = f"{task.label} was cancelled."
                    else:
                        notification = f"{task.label} — {task.status.value}"

                notification = _strip_task_refs_for_chat(notification)
                if not notification.strip():
                    notification = f"{task.label} — {task.status.value}"

                # Persist completion outputs into conversation history so terse
                # follow-ups ("proceed", "do it") still have full context.
                self._persist_completion_context(task, notification)

                await self.bus.publish_outbound(OutboundMessage(
                    channel=task.origin_channel,
                    chat_id=task.origin_chat_id,
                    content=notification,
                    is_background=True,
                ))
                logger.debug(
                    f"Delivered completion for {task.origin_channel}:{task.origin_chat_id}: {task.label}",
                )

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
        and uses structured task telemetry instead of hardcoded templates.
        """
        while self._running:
            try:
                interval = 10.0
                await asyncio.sleep(interval)

                active_tasks = self.registry.get_active_tasks()
                if not active_tasks:
                    continue

                # Group by origin (channel:chat_id)
                by_origin: dict[str, list[Task]] = {}
                for task in active_tasks:
                    if not task.progress_updates_enabled:
                        continue
                    # Skip tasks the narrator is already covering
                    if self.narrator and self.narrator.is_narrating(task.id):
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

                    summaries = []
                    for task in tasks:
                        if task.status == TaskStatus.RUNNING:
                            summaries.append(self._build_status_report(task))

                    if not summaries:
                        continue

                    prev_summaries = self._last_progress_summary.get(origin_key)
                    if prev_summaries == summaries:
                        continue
                    self._last_progress_summary[origin_key] = summaries

                    raw_update = "\n\n".join(summaries)
                    try:
                        update = await self._rewrite_status_report(
                            raw_update,
                            "Live background progress update",
                        )
                        if self._status_rewrite_is_unusable(update or ""):
                            update = self._format_status_fallback(raw_update)
                    except Exception as e:
                        logger.error(
                            f"Failed to rewrite progress update for {origin_key}: {e}",
                        )
                        update = self._format_status_fallback(raw_update)

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
