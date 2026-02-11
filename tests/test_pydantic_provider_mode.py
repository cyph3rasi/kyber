from __future__ import annotations

from pathlib import Path

from kyber.providers.pydantic_provider import PydanticAIProvider


def _provider() -> PydanticAIProvider:
    return PydanticAIProvider(
        api_key="test-key",
        provider_name="openai",
        default_model="openai:gpt-5-mini",
        workspace=Path.cwd(),
    )


def test_provider_native_orchestration_is_always_disabled() -> None:
    p = _provider()
    assert p.uses_provider_native_orchestration() is False


def test_provider_native_session_context_is_disabled() -> None:
    p = _provider()
    assert p.uses_native_session_context() is False
