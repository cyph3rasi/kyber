from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kyber.agent.narrator import LiveNarrator
from kyber.agent.task_registry import TaskRegistry
from kyber.agent.worker import Worker
from kyber.providers.base import LLMProvider, LLMResponse


class CallbackTaskProvider(LLMProvider):
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
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "stub-model"

    async def execute_task(
        self,
        *,
        task_description: str,
        persona_prompt: str,
        timezone: str | None = None,
        workspace: Path | None = None,
        callback: Any | None = None,
    ) -> str:
        _ = (task_description, persona_prompt, timezone, workspace)
        if callback is not None:
            await callback("Using tool: `read_file`...")
            await callback("Using tool: `exec`...")
        return "Completed task successfully after running checks."


class _NarratorStub:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def narrate(self, task_id: str, action: str) -> None:
        self.events.append((task_id, action))


@pytest.mark.asyncio
async def test_worker_execute_uses_provider_task_callback(tmp_path: Path) -> None:
    provider = CallbackTaskProvider()
    registry = TaskRegistry()
    task = registry.create(
        description="do work",
        label="Test Task",
        origin_channel="discord",
        origin_chat_id="room",
    )
    registry.mark_started(task.id)
    narrator = _NarratorStub()

    worker = Worker(
        task=task,
        provider=provider,
        workspace=tmp_path,
        registry=registry,
        completion_queue=asyncio.Queue(),
        persona_prompt="persona",
        narrator=narrator,  # type: ignore[arg-type]
    )

    result = await worker._execute()
    assert "Completed task successfully" in result

    updated = registry.get(task.id)
    assert updated is not None
    assert updated.iteration >= 2
    assert any("read_file" in a for a in updated.actions_completed)
    assert any("exec" in a for a in updated.actions_completed)
    assert any("read_file" in a for _, a in narrator.events)


@pytest.mark.asyncio
async def test_live_narrator_flushes_buffer_on_unregister() -> None:
    sent: list[str] = []

    async def _flush(_channel: str, _chat_id: str, message: str) -> None:
        sent.append(message)

    narrator = LiveNarrator(flush_callback=_flush, flush_interval=60.0)
    task_id = "t1"
    narrator.register_task(task_id, "discord", "room", "Task")
    narrator.narrate(task_id, "Using tool: `read_file`...")
    narrator.unregister_task(task_id)

    await asyncio.sleep(0)
    assert any("Quick update:" in msg for msg in sent)
