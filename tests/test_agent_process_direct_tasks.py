from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from kyber.agent.core import AgentCore
from kyber.agent.task_registry import TaskStatus
from kyber.bus.queue import MessageBus
from kyber.providers.base import LLMProvider, LLMResponse


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
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


def _make_core(workspace: Path) -> AgentCore:
    return AgentCore(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=workspace,
    )


def test_process_direct_auto_completes_auto_created_task() -> None:
    with TemporaryDirectory() as td:
        core = _make_core(Path(td))
        task = core.registry.create(
            description="do thing",
            label="Do Thing",
            origin_channel="cli",
            origin_chat_id="direct",
        )
        core.registry.mark_started(task.id)

        async def _fake_process(*args, **kwargs):
            kwargs["task_tracker"]["id"] = task.id
            return "done"

        core._process_message = _fake_process  # type: ignore[method-assign]
        out = asyncio.run(core.process_direct("hello"))
        assert out == "done"

        refreshed = core.registry.get(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.COMPLETED
        assert refreshed.result == "done"


def test_process_direct_with_external_task_id_does_not_double_finalize() -> None:
    with TemporaryDirectory() as td:
        core = _make_core(Path(td))
        task = core.registry.create(
            description="do thing",
            label="Do Thing",
            origin_channel="cli",
            origin_chat_id="direct",
        )
        core.registry.mark_started(task.id)

        async def _fake_process(*args, **kwargs):
            kwargs["task_tracker"]["id"] = task.id
            return "done"

        core._process_message = _fake_process  # type: ignore[method-assign]
        out = asyncio.run(core.process_direct("hello", tracked_task_id=task.id))
        assert out == "done"

        refreshed = core.registry.get(task.id)
        assert refreshed is not None
        # Caller-managed tracked_task_id should be finalized by the caller.
        assert refreshed.status == TaskStatus.RUNNING


def test_process_direct_auto_marks_failed_on_exception() -> None:
    with TemporaryDirectory() as td:
        core = _make_core(Path(td))
        task = core.registry.create(
            description="do thing",
            label="Do Thing",
            origin_channel="cli",
            origin_chat_id="direct",
        )
        core.registry.mark_started(task.id)

        async def _fake_process(*args, **kwargs):
            kwargs["task_tracker"]["id"] = task.id
            raise RuntimeError("boom")

        core._process_message = _fake_process  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            asyncio.run(core.process_direct("hello"))

        refreshed = core.registry.get(task.id)
        assert refreshed is not None
        assert refreshed.status == TaskStatus.FAILED
        assert refreshed.error == "boom"
