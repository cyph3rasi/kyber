from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kyber.agent.orchestrator import Orchestrator
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class SequenceProvider(LLMProvider):
    """
    Minimal provider stub that returns a fixed sequence of responses.

    Call order for this test:
    1) Orchestrator structured response: tool call "respond" with spawn_task
    2) Worker execution: final content with no tool calls (ends task)
    """

    def __init__(self, responses: list[LLMResponse]):
        super().__init__(api_key=None, api_base=None)
        self._responses = responses[:]
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls.append(
            {"messages": messages, "tools": tools, "model": model, "max_tokens": max_tokens}
        )
        if not self._responses:
            return LLMResponse(content="(no more stubbed responses)")
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "stub-model"


@pytest.mark.asyncio
async def test_process_direct_runs_spawn_task_inline_when_not_running(tmp_path: Path) -> None:
    bus = MessageBus()

    # 1) Orchestrator turn: respond tool call with spawn_task
    tool_call = ToolCallRequest(
        id="call_1",
        name="respond",
        arguments={
            "message": "I'll take care of that.",
            "intent": {
                "action": "spawn_task",
                "task_description": "Do the thing.",
                "task_label": "Test Task",
                "complexity": "simple",
            },
        },
    )
    r1 = LLMResponse(content=None, tool_calls=[tool_call])

    # 2) Worker turn: final answer with no tool calls
    r2 = LLMResponse(content="All done.")

    provider = SequenceProvider([r1, r2])
    agent = Orchestrator(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        persona_prompt="(persona)",
        model="stub-model",
        brave_api_key=None,
    )

    out = await agent.process_direct("please do something", session_key="cli:test")

    # Inline completion should return the worker's result.
    assert "All done." in out
    assert "✅" not in out

    # It should not claim to have spawned a background task reference for later.
    assert "Ref:" not in out
