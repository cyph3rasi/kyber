from __future__ import annotations

from fastapi.testclient import TestClient

from kyber.agent.task_registry import TaskRegistry, TaskStatus
from kyber.gateway.api import create_gateway_app


class _DummyBus:
    def __init__(self) -> None:
        self.outbound: list[object] = []

    async def publish_outbound(self, msg) -> None:
        self.outbound.append(msg)


class _DummyAgent:
    def __init__(self, cancel_returns_true: bool) -> None:
        self.registry = TaskRegistry()
        self.bus = _DummyBus()
        self._cancel_returns_true = cancel_returns_true

    def _cancel_task(self, task_id: str) -> bool:
        if self._cancel_returns_true:
            self.registry.mark_cancelled(task_id, "Cancelled by user")
            return True
        return False


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_running_task(agent: _DummyAgent):
    task = agent.registry.create(
        description="do a thing",
        label="Do Thing",
        origin_channel="discord",
        origin_chat_id="abc123",
    )
    agent.registry.mark_started(task.id)
    return task


def test_cancel_sets_cancelled_and_sends_confirmation_when_cancel_path_succeeds() -> None:
    token = "test-token"
    agent = _DummyAgent(cancel_returns_true=True)
    task = _make_running_task(agent)
    app = create_gateway_app(agent, token)
    client = TestClient(app)

    res = client.post(f"/tasks/{task.reference[1:]}/cancel", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["status"] == TaskStatus.CANCELLED.value

    refreshed = agent.registry.get(task.id)
    assert refreshed is not None
    assert refreshed.status == TaskStatus.CANCELLED

    assert len(agent.bus.outbound) == 1
    outbound = agent.bus.outbound[0]
    assert outbound.channel == "discord"
    assert outbound.chat_id == "abc123"
    assert outbound.is_background is False
    assert "Task cancelled from dashboard" in outbound.content


def test_cancel_force_marks_and_sends_confirmation_when_runner_missing() -> None:
    token = "test-token"
    agent = _DummyAgent(cancel_returns_true=False)
    task = _make_running_task(agent)
    app = create_gateway_app(agent, token)
    client = TestClient(app)

    res = client.post(f"/tasks/{task.reference[1:]}/cancel", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["status"] == TaskStatus.CANCELLED.value

    refreshed = agent.registry.get(task.id)
    assert refreshed is not None
    assert refreshed.status == TaskStatus.CANCELLED

    assert len(agent.bus.outbound) == 1
    outbound = agent.bus.outbound[0]
    assert outbound.channel == "discord"
    assert outbound.chat_id == "abc123"
    assert outbound.is_background is False
    assert "Task cancelled from dashboard" in outbound.content
