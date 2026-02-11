from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kyber.agent.intent import IntentAction, parse_response_content
from kyber.agent.orchestrator import Orchestrator
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse


class JsonContentProvider(LLMProvider):
    def __init__(self, content: str):
        super().__init__(api_key=None, api_base=None)
        self._content = content

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
        return LLMResponse(content=self._content, tool_calls=[])

    def get_default_model(self) -> str:
        return "stub-model"


class JsonContentNativeProvider(JsonContentProvider):
    def uses_provider_native_orchestration(self) -> bool:
        return True


def test_parse_response_content_handles_json_envelope() -> None:
    parsed = parse_response_content(
        '{"message":"hello there","intent":{"action":"none"}}'
    )
    assert parsed is not None
    assert parsed.message == "hello there"
    assert parsed.intent.action == IntentAction.NONE


def test_parse_response_content_handles_fenced_json_envelope() -> None:
    parsed = parse_response_content(
        '```json\n{"message":"wrapped","intent":{"action":"none"}}\n```'
    )
    assert parsed is not None
    assert parsed.message == "wrapped"
    assert parsed.intent.action == IntentAction.NONE


def test_parse_response_content_invalid_action_defaults_to_none() -> None:
    parsed = parse_response_content(
        '{"message":"ok","intent":{"action":"totally_invalid"}}'
    )
    assert parsed is not None
    assert parsed.intent.action == IntentAction.NONE


@pytest.mark.asyncio
async def test_orchestrator_structured_fallback_unwraps_json_content(tmp_path: Path) -> None:
    provider = JsonContentProvider(
        '{"message":"I posted it.","intent":{"action":"none"}}'
    )
    agent = Orchestrator(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        persona_prompt="persona",
        model="stub-model",
    )

    response = await agent._get_structured_response(
        messages=[{"role": "user", "content": "status?"}],
        session_key="discord:room",
    )

    assert response is not None
    assert response.message == "I posted it."
    assert response.intent.action == IntentAction.NONE


@pytest.mark.asyncio
async def test_provider_native_path_unwraps_json_content(tmp_path: Path) -> None:
    provider = JsonContentNativeProvider(
        '{"message":"native plain text","intent":{"action":"none"}}'
    )
    agent = Orchestrator(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        persona_prompt="persona",
        model="stub-model",
    )

    out = await agent.process_direct("hello there", session_key="discord:room")
    assert out == "native plain text"
