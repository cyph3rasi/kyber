from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kyber.agent.intent import AgentResponse, Intent, IntentAction
from kyber.providers.pydantic_provider import PydanticAIProvider


def _provider() -> PydanticAIProvider:
    return PydanticAIProvider(
        api_key="test-key",
        provider_name="openai",
        default_model="openai:gpt-5-mini",
        workspace=Path.cwd(),
    )


@pytest.mark.asyncio
async def test_execute_task_routes_to_task_agent(monkeypatch) -> None:
    p = _provider()

    async def _task_agent(
        *,
        selected_model: str,
        task_description: str,
        system_prompt: str,
        workspace: Path,
        callback=None,
    ) -> str:
        _ = (selected_model, task_description, system_prompt, workspace, callback)
        return "task result"

    monkeypatch.setattr(p, "_run_task_agent", _task_agent)

    out = await p.execute_task(
        task_description="do work",
        persona_prompt="persona",
    )
    assert out == "task result"


@pytest.mark.asyncio
async def test_chat_with_tools_returns_tool_call(monkeypatch) -> None:
    """chat() with tools uses AgentResponse structured output and wraps it as a ToolCallRequest."""
    p = _provider()

    # Mock _run_with_retries to return a fake AgentRunResult with AgentResponse output.
    fake_response = AgentResponse(
        message="hi",
        intent=Intent(action=IntentAction.NONE),
    )
    fake_result = MagicMock()
    fake_result.output = fake_response

    async def _fake_retries(call, *, retries=3):
        return fake_result

    monkeypatch.setattr(p, "_run_with_retries", _fake_retries)

    response = await p.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "respond", "parameters": {"type": "object"}},
            }
        ],
        model="openai:gpt-5-mini",
    )

    assert response.has_tool_calls is True
    assert response.tool_calls[0].name == "respond"
    assert response.tool_calls[0].arguments.get("message") == "hi"
    assert response.tool_calls[0].arguments.get("intent", {}).get("action") == "none"


@pytest.mark.asyncio
async def test_chat_without_tools_returns_text(monkeypatch) -> None:
    """chat() without tools uses _run_text and returns plain text."""
    p = _provider()

    async def _run_text(
        *,
        selected_model: str,
        messages: list[dict],
        max_tokens: int | None,
        temperature: float,
        message_history=None,
    ) -> str:
        return "plain text response"

    monkeypatch.setattr(p, "_run_text", _run_text)

    response = await p.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model="openai:gpt-5-mini",
    )

    assert response.has_tool_calls is False
    assert response.content == "plain text response"
    assert response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_with_tools_error_returns_error_response(monkeypatch) -> None:
    """chat() returns an error LLMResponse when the agent call fails."""
    p = _provider()

    async def _fake_retries(call, *, retries=3):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(p, "_run_with_retries", _fake_retries)

    response = await p.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "respond", "parameters": {"type": "object"}},
            }
        ],
        model="openai:gpt-5-mini",
    )

    assert response.has_tool_calls is False
    assert "Error calling LLM" in (response.content or "")
    assert response.finish_reason == "error"
