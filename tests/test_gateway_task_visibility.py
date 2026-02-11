from __future__ import annotations

from datetime import datetime

from kyber.agent.task_registry import Task, TaskStatus
from kyber.gateway.api import _is_dashboard_visible_task


def _task(
    *,
    origin_channel: str = "discord",
    origin_chat_id: str = "123",
    label: str = "User task",
    description: str = "Do work",
    status: TaskStatus = TaskStatus.COMPLETED,
) -> Task:
    return Task(
        id="abc12345",
        reference="⚡deadbeef",
        description=description,
        label=label,
        status=status,
        origin_channel=origin_channel,
        origin_chat_id=origin_chat_id,
        created_at=datetime.now(),
        completed_at=datetime.now(),
    )


def test_dashboard_visibility_keeps_user_tasks() -> None:
    t = _task(origin_channel="discord", origin_chat_id="555")
    assert _is_dashboard_visible_task(t) is True


def test_dashboard_visibility_hides_internal_origin_tasks() -> None:
    t = _task(origin_channel="internal", origin_chat_id="heartbeat")
    assert _is_dashboard_visible_task(t) is False


def test_dashboard_visibility_hides_legacy_heartbeat_entries() -> None:
    t = _task(
        origin_channel="cli",
        origin_chat_id="heartbeat",
        label="Heartbeat check",
        description="Read HEARTBEAT.md in your workspace. Reply with HEARTBEAT_OK",
    )
    assert _is_dashboard_visible_task(t) is False
