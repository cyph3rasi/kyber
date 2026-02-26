"""
Live Narrator: Streams agent actions to the user in real-time.

Each action the agent takes is sent immediately as it happens,
styled with emoji and laymens-term descriptions so non-technical
users can follow along while techie users still see the raw commands.

No batching, no LLM calls — just instant, styled action labels.
"""

import asyncio
from typing import Callable, Awaitable

from loguru import logger


class LiveNarrator:
    """
    Streams each agent action to the user the instant it happens.

    No buffering or batching — every action label is sent immediately
    via the flush callback.  Consecutive duplicate labels are suppressed
    to avoid spam when the agent retries the same tool call.
    """

    def __init__(
        self,
        flush_callback: Callable[[str, str, str], Awaitable[None]],
        *,
        flush_interval: float = 0.0,  # kept for backwards-compat; ignored
        persona_name: str = "",
    ):
        """
        Args:
            flush_callback: async fn(channel, chat_id, message) to send updates
            flush_interval: ignored (kept for API compatibility)
            persona_name: unused (kept for API compatibility)
        """
        self._callback = flush_callback
        # task_id → (channel, chat_id)
        self._origins: dict[str, tuple[str, str]] = {}
        # task_id → task label for friendlier intro text
        self._labels: dict[str, str] = {}
        # Track whether we already sent the kickoff ack.
        self._intro_sent: set[str] = set()
        # task_id → last label sent (for dedup)
        self._last_label: dict[str, str] = {}

    def start(self) -> None:
        """No-op (kept for API compatibility — no background loop needed)."""

    def stop(self) -> None:
        """No-op (kept for API compatibility)."""

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

    async def flush_and_unregister(self, task_id: str) -> None:
        """Clean up task state.  Nothing to flush — actions are sent instantly."""
        self._origins.pop(task_id, None)
        self._labels.pop(task_id, None)
        self._intro_sent.discard(task_id)
        self._last_label.pop(task_id, None)

    def unregister_task(self, task_id: str) -> None:
        """Clean up a task's narrator state (non-blocking sync version)."""
        self._origins.pop(task_id, None)
        self._labels.pop(task_id, None)
        self._intro_sent.discard(task_id)
        self._last_label.pop(task_id, None)

    def is_narrating(self, task_id: str) -> bool:
        """Check if a task is currently being narrated (registered and active)."""
        return task_id in self._origins

    async def narrate(self, task_id: str, action: str) -> None:
        """
        Send a single action label to the user immediately.

        Consecutive duplicate labels are suppressed to avoid spam.
        """
        if not action or not action.strip():
            return

        origin = self._origins.get(task_id)
        if not origin:
            return

        channel, chat_id = origin
        if channel == "dashboard":
            return

        # Suppress consecutive identical labels
        if self._last_label.get(task_id) == action:
            return
        self._last_label[task_id] = action

        await self._send_safe(channel, chat_id, action, task_id)

    async def _send_safe(self, channel: str, chat_id: str, message: str, task_id: str) -> None:
        try:
            await self._callback(channel, chat_id, message)
        except Exception as e:
            logger.warning(f"Failed to send narration for task {task_id}: {e}")

    @staticmethod
    def _build_intro(label: str) -> str:
        return (
            f"starting {label} now. i'll drop in when it's done."
        )
