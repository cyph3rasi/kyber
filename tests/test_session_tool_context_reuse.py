from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from kyber.agent.core import AgentCore
from kyber.bus.events import InboundMessage
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse
from kyber.session.manager import Session


class _DummyProvider(LLMProvider):
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
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


def _make_core(workspace: Path) -> AgentCore:
    core = AgentCore(bus=MessageBus(), provider=_DummyProvider(), workspace=workspace)
    core.sessions.save = lambda _session: None  # type: ignore[method-assign]
    return core


def test_build_messages_includes_recent_tool_context_block() -> None:
    with TemporaryDirectory() as td:
        core = _make_core(Path(td))
        session = Session(key="discord:test")
        session.add_message("user", "list files")
        session.add_message("tool", "README.md src tests", tool_name="list_dir", tool_call_id="t1")
        session.add_message("assistant", "Found files.")

        messages = core._build_messages("system prompt", session)

        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "system prompt"
        assert messages[1]["role"] == "system"
        assert "Recent tool outputs from this chat" in messages[1]["content"]
        assert "[list_dir]" in messages[1]["content"]
        assert any(m["role"] == "user" for m in messages)
        assert any(m["role"] == "assistant" for m in messages)


def test_process_message_persists_turn_tool_messages_into_shared_session() -> None:
    async def _run() -> None:
        with TemporaryDirectory() as td:
            core = _make_core(Path(td))

            async def _fake_run_loop(*_args, **kwargs):
                sess = kwargs["session"]
                sess.add_message("tool", "README.md src tests", tool_name="list_dir", tool_call_id="tc-1")
                return "done"

            async def _fake_status_intro(*_args, **_kwargs):
                return "✅ Task: test"

            core._run_loop = _fake_run_loop  # type: ignore[method-assign]
            core._build_status_intro = _fake_status_intro  # type: ignore[method-assign]

            msg = InboundMessage(channel="discord", sender_id="u1", chat_id="c1", content="check repo")
            out = await core._process_message(msg, "discord:c1", session_lock_held=False)
            assert out == "done"

            shared = core.sessions.get_or_create("discord:c1")
            tool_entries = [m for m in shared.messages if m.get("role") == "tool"]
            assert len(tool_entries) >= 1
            assert tool_entries[-1].get("tool_name") == "list_dir"
            assert "README.md" in str(tool_entries[-1].get("content"))

    asyncio.run(_run())
