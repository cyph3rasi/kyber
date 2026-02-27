from __future__ import annotations

import asyncio
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from kyber.agent.core import AgentCore
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from kyber.session.manager import Session


class _SequencedProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key=None, api_base=None)
        self._responses = list(responses)

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
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "dummy"


def test_status_intro_emits_immediately_before_tool_updates() -> None:
    async def _run() -> None:
        with TemporaryDirectory() as td:
            events: list[str] = []

            async def _progress(
                channel: str,
                chat_id: str,
                status_line: str,
                status_key: str = "",
            ) -> None:
                del channel, chat_id, status_key
                events.append(status_line)

            provider = _SequencedProvider(
                [
                    LLMResponse(
                        content=None,
                        tool_calls=[
                            ToolCallRequest(
                                id="tc1",
                                name="read_file",
                                arguments={"path": "/tmp/test.txt"},
                            )
                        ],
                    ),
                    LLMResponse(content="done"),
                ]
            )
            core = AgentCore(
                bus=MessageBus(),
                provider=provider,
                workspace=Path(td),
                progress_callback=_progress,
            )

            async def _fake_execute_tool(*args, **kwargs) -> str:
                del args, kwargs
                return "{\"ok\": true}"

            core._execute_tool = _fake_execute_tool  # type: ignore[method-assign]

            out = await core._run_loop(
                messages=[{"role": "user", "content": "read a file"}],
                tools=[],
                session=Session(key="discord:1"),
                context_channel="discord",
                context_chat_id="1",
                context_status_key="k1",
                context_status_intro="✅ Task: read a file",
            )

            assert out == "done"
            assert events[0] == "__KYBER_STATUS_START__"
            assert events[1] == "✅ Task: read a file"
            assert events[-1] == "__KYBER_STATUS_END__"

            tool_line = next(
                e for e in events if e not in {"__KYBER_STATUS_START__", "__KYBER_STATUS_END__"} and not e.startswith("✅ Task:")
            )
            assert events.index("✅ Task: read a file") < events.index(tool_line)
            assert re.search(r"\\b\\d+(?:\\.\\d+)?s\\b|\\b\\d+ms\\b", tool_line) is None

    asyncio.run(_run())


def test_status_intro_emits_for_no_tool_response() -> None:
    async def _run() -> None:
        with TemporaryDirectory() as td:
            events: list[str] = []

            async def _progress(
                channel: str,
                chat_id: str,
                status_line: str,
                status_key: str = "",
            ) -> None:
                del channel, chat_id, status_key
                events.append(status_line)

            provider = _SequencedProvider([LLMResponse(content="done")])
            core = AgentCore(
                bus=MessageBus(),
                provider=provider,
                workspace=Path(td),
                progress_callback=_progress,
            )

            out = await core._run_loop(
                messages=[{"role": "user", "content": "say done"}],
                tools=[],
                session=Session(key="discord:1"),
                context_channel="discord",
                context_chat_id="1",
                context_status_key="k2",
                context_status_intro="✅ Task: say done",
            )

            assert out == "done"
            assert events == [
                "__KYBER_STATUS_START__",
                "✅ Task: say done",
                "__KYBER_STATUS_END__",
            ]

    asyncio.run(_run())
