from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from kyber.agent.core import AgentCore
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse


class _SummaryProvider(LLMProvider):
    def __init__(self, content: str | None = None, should_fail: bool = False) -> None:
        super().__init__(api_key=None, api_base=None)
        self._content = content
        self._should_fail = should_fail

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        tool_choice: Any | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        del messages, tools, model, tool_choice, max_tokens, temperature
        if self._should_fail:
            raise RuntimeError("provider unavailable")
        return LLMResponse(content=self._content)

    def get_default_model(self) -> str:
        return "dummy"


def _make_core(workspace: Path, provider: LLMProvider) -> AgentCore:
    return AgentCore(bus=MessageBus(), provider=provider, workspace=workspace)


def test_status_intro_uses_llm_rephrase_and_prefix() -> None:
    with TemporaryDirectory() as td:
        core = _make_core(Path(td), _SummaryProvider("✅ Task: Summarize inbox every 3 hours."))
        intro = asyncio.run(core._build_status_intro("read my inbox and summarize it every 3 hours"))
        assert intro == "✅ Task: Summarize inbox every 3 hours."


def test_status_intro_falls_back_when_summary_fails() -> None:
    with TemporaryDirectory() as td:
        core = _make_core(Path(td), _SummaryProvider(should_fail=True))
        intro = asyncio.run(core._build_status_intro("   set up hourly digest for alerts   "))
        assert intro == "✅ Task: set up hourly digest for alerts"


def test_status_intro_empty_content() -> None:
    with TemporaryDirectory() as td:
        core = _make_core(Path(td), _SummaryProvider(None))
        intro = asyncio.run(core._build_status_intro(""))
        assert intro == "✅ Task: In progress."
