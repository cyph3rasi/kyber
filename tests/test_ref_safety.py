from __future__ import annotations

import re
from pathlib import Path

import pytest

from kyber.agent.intent import AgentResponse, IntentAction
from kyber.agent.orchestrator import Orchestrator, _strip_fabricated_refs
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse


class NullProvider(LLMProvider):
    async def chat(self, *args, **kwargs) -> LLMResponse:  # type: ignore[override]
        return LLMResponse(content="(unused)")

    def get_default_model(self) -> str:  # type: ignore[override]
        return "stub"


def test_strip_fabricated_refs_variants() -> None:
    msg = "hello\n\nRef: ⚡e8c47a9d\nbye"
    assert "Ref:" not in _strip_fabricated_refs(msg)
    assert "e8c47a9d" not in _strip_fabricated_refs(msg)

    msg2 = "hello\n\nRef:\n\n7ab39f52\n\nbye"
    cleaned2 = _strip_fabricated_refs(msg2)
    assert "Ref:" not in cleaned2
    assert "7ab39f52" not in cleaned2

    msg3 = "hello\n\n⚡deadbeef\n\nbye"
    cleaned3 = _strip_fabricated_refs(msg3)
    assert "deadbeef" not in cleaned3


@pytest.mark.asyncio
async def test_execute_intent_none_strips_refs(tmp_path: Path) -> None:
    agent = Orchestrator(
        bus=MessageBus(),
        provider=NullProvider(),
        workspace=tmp_path,
        persona_prompt="(persona)",
        model="stub",
        brave_api_key=None,
    )

    resp = AgentResponse(
        message="sure.\n\nRef: ⚡e8c47a9d",
        action=IntentAction.NONE,
    )
    out = await agent._execute_intent(resp, channel="discord", chat_id="123")
    assert "Ref:" not in out
    assert "e8c47a9d" not in out


@pytest.mark.asyncio
async def test_execute_intent_spawn_task_strips_model_ref_and_injects_real_ref(tmp_path: Path) -> None:
    agent = Orchestrator(
        bus=MessageBus(),
        provider=NullProvider(),
        workspace=tmp_path,
        persona_prompt="(persona)",
        model="stub",
        brave_api_key=None,
    )
    agent._running = True  # emulate gateway mode

    # Prevent actual worker tasks from being created; we only care about message/ref injection.
    agent.workers.spawn = lambda task: None  # type: ignore[assignment]

    resp = AgentResponse(
        message="I'll investigate.\n\nRef: ⚡deadbeef",
        action=IntentAction.SPAWN_TASK,
        task_description="Investigate.",
        task_label="Investigate",
        complexity="simple",
    )
    out = await agent._execute_intent(resp, channel="discord", chat_id="123")

    assert "deadbeef" not in out
    # Refs are now internal-only (not shown in chat). Depending on execution
    # mode, the task may still be active or already completed inline.
    active = agent.registry.get_active_tasks()
    if active:
        assert len(active) == 1
        assert active[0].label == "Investigate"
        assert active[0].reference.startswith("⚡")
    else:
        history = agent.registry.get_history(limit=5)
        assert any(t.label == "Investigate" for t in history)
