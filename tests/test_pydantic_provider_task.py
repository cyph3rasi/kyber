from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kyber.agent.intent import AgentResponse, Intent, IntentAction
from kyber.providers.pydantic_provider import PydanticAIProvider
from kyber.providers.history import dicts_to_model_messages, model_messages_to_dicts


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


# --- run_structured() tests ---


@pytest.mark.asyncio
async def test_run_structured_returns_agent_response(monkeypatch) -> None:
    """run_structured() returns an AgentResponse directly via structured output."""
    p = _provider()

    fake_response = AgentResponse(
        message="structured reply",
        intent=Intent(action=IntentAction.NONE),
    )
    fake_result = MagicMock()
    fake_result.output = fake_response

    async def _fake_retries(call, *, retries=3):
        return fake_result

    monkeypatch.setattr(p, "_run_with_retries", _fake_retries)

    result = await p.run_structured(
        instructions="You are a helpful assistant.",
        user_message="hello",
        model="openai:gpt-5-mini",
    )

    assert isinstance(result, AgentResponse)
    assert result.message == "structured reply"
    assert result.intent.action == IntentAction.NONE


@pytest.mark.asyncio
async def test_run_structured_with_message_history(monkeypatch) -> None:
    """run_structured() passes message_history through to the agent."""
    p = _provider()

    fake_response = AgentResponse(
        message="with history",
        intent=Intent(action=IntentAction.NONE),
    )
    fake_result = MagicMock()
    fake_result.output = fake_response

    captured_calls = []

    async def _fake_retries(call, *, retries=3):
        captured_calls.append(call)
        return fake_result

    monkeypatch.setattr(p, "_run_with_retries", _fake_retries)

    history = dicts_to_model_messages([
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ])

    result = await p.run_structured(
        instructions="system prompt",
        user_message="follow-up",
        message_history=history,
        model="openai:gpt-5-mini",
    )

    assert result.message == "with history"
    assert len(captured_calls) == 1


@pytest.mark.asyncio
async def test_run_structured_raises_without_model() -> None:
    """run_structured() raises RuntimeError when no model is configured."""
    p = PydanticAIProvider(
        api_key="test-key",
        provider_name="openai",
        default_model="",
        workspace=Path.cwd(),
    )

    with pytest.raises(RuntimeError, match="No model configured"):
        await p.run_structured(
            instructions="system",
            user_message="hello",
            model="",
        )


# --- Message history conversion tests ---


def test_dicts_to_model_messages_basic_round_trip() -> None:
    """dicts_to_model_messages and model_messages_to_dicts round-trip correctly."""
    original = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "how are you?"},
    ]

    model_msgs = dicts_to_model_messages(original)
    assert len(model_msgs) == 3

    back = model_messages_to_dicts(model_msgs)
    assert len(back) == 3
    assert back[0] == {"role": "user", "content": "hello"}
    assert back[1] == {"role": "assistant", "content": "hi there"}
    assert back[2] == {"role": "user", "content": "how are you?"}


def test_dicts_to_model_messages_empty_list() -> None:
    """Empty input produces empty output."""
    assert dicts_to_model_messages([]) == []
    assert model_messages_to_dicts([]) == []


def test_dicts_to_model_messages_skips_none_content() -> None:
    """Messages with None content are skipped."""
    msgs = [
        {"role": "user", "content": None},
        {"role": "user", "content": "real message"},
    ]
    result = dicts_to_model_messages(msgs)
    assert len(result) == 1

    back = model_messages_to_dicts(result)
    assert len(back) == 1
    assert back[0]["content"] == "real message"


def test_dicts_to_model_messages_skips_system_messages() -> None:
    """System messages are skipped (should be passed via instructions)."""
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hello"},
    ]
    result = dicts_to_model_messages(msgs)
    assert len(result) == 1

    back = model_messages_to_dicts(result)
    assert back[0] == {"role": "user", "content": "hello"}


def test_dicts_to_model_messages_skips_unknown_roles() -> None:
    """Messages with unknown roles are skipped."""
    msgs = [
        {"role": "function", "content": "some output"},
        {"role": "user", "content": "hello"},
    ]
    result = dicts_to_model_messages(msgs)
    assert len(result) == 1
