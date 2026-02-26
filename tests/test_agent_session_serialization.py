from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any

from kyber.agent.core import AgentCore
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test", api_base=None)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        tool_choice: Any | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        del tools, model, tool_choice, max_tokens, temperature
        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_text = str(msg.get("content") or "")
                break
        # Simulate a slow first request.
        if "slow" in user_text:
            await asyncio.sleep(0.20)
        return LLMResponse(content=f"ok:{user_text}")

    def get_default_model(self) -> str:
        return "stub-model"


def _make_agent() -> AgentCore:
    workspace = Path(tempfile.mkdtemp(prefix="kyber-agent-test-"))
    return AgentCore(
        bus=MessageBus(),
        provider=_StubProvider(),
        workspace=workspace,
        model="stub-model",
    )


def test_process_direct_serialized_within_same_session() -> None:
    agent = _make_agent()
    completion_order: list[str] = []

    async def _run() -> None:
        async def call(text: str) -> str:
            out = await agent.process_direct(text, session_key="discord:chat-1")
            completion_order.append(text)
            return out

        first = asyncio.create_task(call("slow first"))
        # Ensure second call attempts to overlap with the first.
        await asyncio.sleep(0.01)
        second = asyncio.create_task(call("fast second"))
        out1, out2 = await asyncio.gather(first, second)
        assert out1 == "ok:slow first"
        assert out2 == "ok:fast second"

    asyncio.run(_run())
    assert completion_order == ["slow first", "fast second"]


def test_process_direct_parallel_across_different_sessions() -> None:
    agent = _make_agent()

    async def _run() -> None:
        start = time.perf_counter()
        first = asyncio.create_task(agent.process_direct("slow one", session_key="discord:chat-a"))
        second = asyncio.create_task(agent.process_direct("slow two", session_key="discord:chat-b"))
        await asyncio.gather(first, second)
        elapsed = time.perf_counter() - start
        # Two 0.20s calls in parallel should be well below 0.40s with margin.
        assert elapsed < 0.34

    asyncio.run(_run())
