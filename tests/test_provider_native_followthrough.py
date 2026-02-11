from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kyber.agent.orchestrator import Orchestrator
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse


class NativePromiseProvider(LLMProvider):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        tool_choice: Any | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        _ = (messages, tools, model, tool_choice, max_tokens, temperature)
        return LLMResponse(
            content=(
                "You're absolutely right. I'll spawn a task to improve the feature "
                "rotation logic so similar tweets are avoided."
            )
        )

    def get_default_model(self) -> str:
        return "stub-model"

    def uses_provider_native_orchestration(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_provider_native_deferred_promise_forces_follow_through_task(tmp_path: Path) -> None:
    provider = NativePromiseProvider()
    agent = Orchestrator(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        persona_prompt="persona",
        model="stub-model",
    )

    executed = []

    async def _capture_inline(task):
        executed.append(task)
        agent.registry.mark_started(task.id)
        agent.registry.mark_completed(task.id, "Done: improved rotation logic.")
        return task

    agent.workers.run_inline = _capture_inline  # type: ignore[assignment]

    out = await agent.process_direct(
        "the tweet was nearly identical to your previous tweet.. dont we have tracking?",
        session_key="discord:room",
        channel="discord",
        chat_id="room",
    )

    assert out == "Done: improved rotation logic."
    assert len(executed) == 1
    assert "follow through" in executed[0].description.lower()
