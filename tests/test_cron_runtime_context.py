from __future__ import annotations

import pytest

from kyber.cron.runtime import resolve_job_context
from kyber.cron.service import CronService
from kyber.cron.types import CronJob, CronPayload, CronSchedule


def test_resolve_job_context_prefers_persisted_session_key() -> None:
    job = CronJob(
        id="job-1",
        name="job-1",
        payload=CronPayload(
            message="ping",
            deliver=True,
            channel="discord",
            to="12345",
            session_key="discord:origin-chat",
        ),
    )

    session_key, channel, chat_id = resolve_job_context(job)
    assert session_key == "discord:origin-chat"
    assert channel == "discord"
    assert chat_id == "12345"


def test_resolve_job_context_uses_delivery_chat_when_no_session_key() -> None:
    job = CronJob(
        id="job-2",
        name="job-2",
        payload=CronPayload(
            message="ping",
            deliver=True,
            channel="discord",
            to="777",
        ),
    )

    session_key, channel, chat_id = resolve_job_context(job)
    assert session_key == "discord:777"
    assert channel == "discord"
    assert chat_id == "777"


def test_resolve_job_context_falls_back_to_cron_session() -> None:
    job = CronJob(
        id="job-3",
        name="job-3",
        payload=CronPayload(message="ping"),
    )

    session_key, channel, chat_id = resolve_job_context(job)
    assert session_key == "cron:job-3"
    assert channel == "cli"
    assert chat_id == "direct"


def test_cron_service_persists_session_key(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    svc = CronService(store_path)

    svc.add_job(
        name="contextful job",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="do work",
        session_key="discord:1374747874885369889",
    )

    # New service instance forces a disk reload.
    reloaded = CronService(store_path)
    jobs = reloaded.list_jobs(include_disabled=True)
    assert len(jobs) == 1
    assert jobs[0].payload.session_key == "discord:1374747874885369889"


@pytest.mark.asyncio
async def test_running_service_reload_keeps_external_delete(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    runner = CronService(store_path)
    job = runner.add_job(
        name="delete me",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="ping",
        job_id="job-delete",
    )

    editor = CronService(store_path)
    assert editor.remove_job(job.id) is True

    await runner._on_timer()

    reloaded = CronService(store_path)
    assert reloaded.list_jobs(include_disabled=True) == []


@pytest.mark.asyncio
async def test_running_service_reload_keeps_external_update(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    runner = CronService(store_path)
    job = runner.add_job(
        name="before",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="old message",
        job_id="job-update",
    )

    editor = CronService(store_path)
    updated = editor.update_job(
        job.id,
        name="after",
        message="new message",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *"),
    )
    assert updated is not None

    await runner._on_timer()

    reloaded = CronService(store_path)
    jobs = reloaded.list_jobs(include_disabled=True)
    assert len(jobs) == 1
    assert jobs[0].name == "after"
    assert jobs[0].payload.message == "new message"
    assert jobs[0].schedule.kind == "cron"
    assert jobs[0].schedule.expr == "0 9 * * *"
