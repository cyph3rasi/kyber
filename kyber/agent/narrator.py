"""
Live Narrator: Streams worker actions to the user in real-time.

Instead of:
  - User waits 30-60 seconds in silence
  - Gets a wall of text at the end
  - Or gets a voice-generated progress ping every 60s (which costs an LLM call)

Now:
  - User sees each action as it happens: "Reading config.py...", "Writing output.txt..."
  - No LLM calls needed — actions are narrated from tool metadata
  - Batched into short bursts so we don't spam the channel

This replaces the voice-based progress loop entirely for active tasks.
"""

import asyncio
import re
import time
from collections import defaultdict
from typing import Callable, Awaitable

from loguru import logger


class LiveNarrator:
    """
    Collects worker actions and flushes them to the user periodically.

    Actions are buffered and sent as a single message every few seconds,
    so the user sees a live feed without getting spammed.
    """

    def __init__(
        self,
        flush_callback: Callable[[str, str, str], Awaitable[None]],
        flush_interval: float = 45.0,
        persona_name: str = "",
    ):
        """
        Args:
            flush_callback: async fn(channel, chat_id, message) to send updates
            flush_interval: seconds between flushes (lower = more real-time)
            persona_name: bot name for light personality in narration
        """
        self._callback = flush_callback
        self._interval = flush_interval
        self._persona = persona_name
        # task_id → list of (timestamp, action_text)
        self._buffers: dict[str, list[tuple[float, str]]] = defaultdict(list)
        # task_id → (channel, chat_id)
        self._origins: dict[str, tuple[str, str]] = {}
        # task_id → task label for friendlier status text
        self._labels: dict[str, str] = {}
        # Track whether we already sent the kickoff ack.
        self._intro_sent: set[str] = set()
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the flush loop."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._flush_loop())

    def stop(self) -> None:
        """Stop the flush loop."""
        self._running = False
        if self._task:
            self._task.cancel()

    def register_task(self, task_id: str, channel: str, chat_id: str, label: str = "") -> None:
        """Register a task's origin for routing narration messages."""
        self._origins[task_id] = (channel, chat_id)
        self._labels[task_id] = label.strip() or "this task"
        if task_id in self._intro_sent:
            return
        if channel == "dashboard":
            return
        self._intro_sent.add(task_id)
        intro = self._build_intro(self._labels[task_id])
        asyncio.create_task(self._send_safe(channel, chat_id, intro, task_id))

    def unregister_task(self, task_id: str) -> None:
        """Clean up when a task completes."""
        actions = self._buffers.pop(task_id, [])
        origin = self._origins.get(task_id)
        if actions and origin:
            channel, chat_id = origin
            if channel != "dashboard":
                message = self._format_actions(actions)
                if message:
                    asyncio.create_task(self._send_safe(channel, chat_id, message, task_id))

        self._origins.pop(task_id, None)
        self._labels.pop(task_id, None)
        self._intro_sent.discard(task_id)

    def is_narrating(self, task_id: str) -> bool:
        """Check if a task is currently being narrated (registered and active)."""
        return task_id in self._origins

    def narrate(self, task_id: str, action: str) -> None:
        """
        Record an action for narration.

        This is called synchronously from the worker's tool execution path.
        The flush loop will batch and send these asynchronously.
        """
        self._buffers[task_id].append((time.time(), action))

    async def _flush_loop(self) -> None:
        """Periodically flush buffered actions to users."""
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                await self._flush_all()
            except asyncio.CancelledError:
                # Final flush on shutdown
                await self._flush_all()
                break
            except Exception as e:
                logger.error(f"Narrator flush error: {e}")

    async def _flush_all(self) -> None:
        """Flush all buffered actions."""
        for task_id in list(self._buffers.keys()):
            actions = self._buffers.pop(task_id, [])
            if not actions:
                continue

            origin = self._origins.get(task_id)
            if not origin:
                continue

            channel, chat_id = origin

            # Skip dashboard — it has its own polling
            if channel == "dashboard":
                continue

            # Format the actions into a compact update
            message = self._format_actions(actions)
            if message:
                await self._send_safe(channel, chat_id, message, task_id)

    async def _send_safe(self, channel: str, chat_id: str, message: str, task_id: str) -> None:
        try:
            await self._callback(channel, chat_id, message)
        except Exception as e:
            logger.warning(f"Failed to send narration for task {task_id}: {e}")

    @staticmethod
    def _extract_command(action: str) -> str:
        m = re.search(r"`([^`]+)`", action or "")
        if m:
            return m.group(1).strip()
        lower = (action or "").lower()
        if lower.startswith("running "):
            return action[len("running ") :].strip()
        return ""

    @staticmethod
    def _extract_file_path(action: str) -> str:
        m = re.search(r"`([^`]+)`", action or "")
        if m:
            return m.group(1).strip()
        return ""

    @staticmethod
    def _build_intro(label: str) -> str:
        return (
            f"On it — I'll start working on {label} now and keep you updated."
        )

    @staticmethod
    def _summarize_intent(actions: list[str]) -> str:
        if not actions:
            return "I'm actively working through the task."
        lower = [a.lower() for a in actions]
        run_count = sum(1 for a in lower if a.startswith("running"))
        read_count = sum(1 for a in lower if a.startswith(("reading", "checking", "globbing", "grepping", "searching")))
        edit_count = sum(1 for a in lower if a.startswith(("writing", "editing")))
        web_count = sum(1 for a in lower if a.startswith("opening"))

        if edit_count > 0:
            return "I'm applying code/content changes and validating that the updates are correct."
        if run_count > 0 and read_count > 0:
            return "I'm inspecting files and running checks to verify how this flow behaves and where to change it."
        if run_count > 0:
            return "I'm running targeted commands to validate the current behavior and gather evidence."
        if read_count > 0:
            return "I'm inspecting the relevant files and wiring to confirm exactly how this works."
        if web_count > 0:
            return "I'm pulling reference material needed to complete this task correctly."
        return "I'm actively working through the task."

    def _format_actions(self, actions: list[tuple[float, str]]) -> str:
        """Format buffered actions into an intent summary + grouped command/file list."""
        if not actions:
            return ""

        # Deduplicate similar consecutive actions
        unique: list[str] = []
        for _, text in actions:
            if not unique or unique[-1] != text:
                unique.append(text)

        recent = unique[-16:]
        commands: list[str] = []
        files: list[str] = []
        for action in recent:
            a = action.lower()
            if a.startswith("running"):
                cmd = self._extract_command(action)
                if cmd and cmd not in commands:
                    commands.append(cmd)
            elif a.startswith(("reading", "writing", "editing", "checking")):
                path = self._extract_file_path(action)
                if path and path not in files:
                    files.append(path)

        parts: list[str] = []
        parts.append(f"Quick update: {self._summarize_intent(recent)}")

        if commands:
            parts.append("Commands I ran:")
            for cmd in commands[:8]:
                parts.append(f"- `{cmd}`")

        if files:
            parts.append("Files I checked:")
            for path in files[:6]:
                parts.append(f"- `{path}`")

        if not commands and not files:
            for action in recent[-6:]:
                parts.append(f"- {action}")

        return "\n".join(parts)
