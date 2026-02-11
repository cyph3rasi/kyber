from __future__ import annotations

from pathlib import Path

import pytest

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
    p = _provider()

    async def _tool_selection(
        *,
        selected_model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int | None,
        temperature: float,
    ):
        _ = (selected_model, messages, tools, max_tokens, temperature)
        from kyber.providers.pydantic_provider import _ToolEnvelope

        return _ToolEnvelope(name="respond", arguments={"message": "hi", "intent": {"action": "none"}})

    monkeypatch.setattr(p, "_run_tool_selection", _tool_selection)

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


@pytest.mark.asyncio
async def test_chat_with_invalid_respond_args_falls_back_to_text(monkeypatch) -> None:
    p = _provider()

    async def _tool_selection(
        *,
        selected_model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int | None,
        temperature: float,
    ):
        _ = (selected_model, messages, tools, max_tokens, temperature)
        from kyber.providers.pydantic_provider import _ToolEnvelope

        # Missing required "message" field.
        return _ToolEnvelope(name="respond", arguments={"intent": {"action": "none"}})

    async def _run_text(
        *,
        selected_model: str,
        messages: list[dict],
        max_tokens: int | None,
        temperature: float,
    ) -> str:
        _ = (selected_model, messages, max_tokens, temperature)
        return "fallback response"

    monkeypatch.setattr(p, "_run_tool_selection", _tool_selection)
    monkeypatch.setattr(p, "_run_text", _run_text)

    response = await p.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "respond",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "intent": {"type": "object"},
                        },
                        "required": ["message", "intent"],
                    },
                },
            }
        ],
        model="openai:gpt-5-mini",
    )

    assert response.has_tool_calls is True
    assert response.tool_calls[0].name == "respond"
    assert response.tool_calls[0].arguments.get("message") == "fallback response"
    assert response.tool_calls[0].arguments.get("intent", {}).get("action") == "none"
