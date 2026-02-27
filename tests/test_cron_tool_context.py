from __future__ import annotations

import json

from kyber.agent.tools import cron as cron_tool
from kyber.cron.types import CronJob, CronJobState, CronPayload, CronSchedule


class _FakeCronService:
    def __init__(self) -> None:
        self.last_add_kwargs: dict | None = None

    def add_job(self, **kwargs):
        self.last_add_kwargs = kwargs
        return CronJob(
            id="job-ctx",
            name=kwargs["name"],
            schedule=kwargs["schedule"],
            payload=CronPayload(
                message=kwargs["message"],
                deliver=kwargs["deliver"],
                channel=kwargs.get("channel"),
                to=kwargs.get("to"),
                session_key=kwargs.get("session_key"),
            ),
            state=CronJobState(next_run_at_ms=1_700_000_000_000),
        )


async def test_schedule_cronjob_captures_origin_context(monkeypatch) -> None:
    fake = _FakeCronService()
    monkeypatch.setattr(cron_tool, "_get_cron_service", lambda: fake)

    tool = cron_tool.ScheduleCronjobTool()
    out = await tool.execute(
        prompt="check inbox",
        schedule="every 2h",
        name="Inbox checker",
        deliver="origin",
        session_key="discord:1374747874885369889",
        context_channel="discord",
        context_chat_id="1374747874885369889",
    )
    payload = json.loads(out)

    assert payload["status"] == "scheduled"
    assert fake.last_add_kwargs is not None
    assert fake.last_add_kwargs["session_key"] == "discord:1374747874885369889"
    assert fake.last_add_kwargs["deliver"] is True
    assert fake.last_add_kwargs["channel"] == "discord"
    assert fake.last_add_kwargs["to"] == "1374747874885369889"


async def test_schedule_cronjob_requires_chat_id_for_platform_delivery(monkeypatch) -> None:
    fake = _FakeCronService()
    monkeypatch.setattr(cron_tool, "_get_cron_service", lambda: fake)

    tool = cron_tool.ScheduleCronjobTool()
    out = await tool.execute(
        prompt="check inbox",
        schedule="every 2h",
        deliver="discord",
    )
    payload = json.loads(out)

    assert "error" in payload
    assert "chat ID" in payload["error"]
