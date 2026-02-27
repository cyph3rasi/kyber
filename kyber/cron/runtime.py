"""Runtime helpers for cron job execution context."""

from kyber.cron.types import CronJob


def resolve_job_context(job: CronJob) -> tuple[str, str, str]:
    """Resolve (session_key, channel, chat_id) for a cron run.

    Priority:
    1) Explicit persisted session key from the scheduled job payload.
    2) Delivered jobs reuse the delivery chat session.
    3) Fallback to a dedicated cron session key.
    """
    channel = "cli"
    chat_id = "direct"

    if job.payload.deliver and job.payload.to:
        channel = (job.payload.channel or "discord").strip() or "discord"
        chat_id = str(job.payload.to)

    persisted_session = (job.payload.session_key or "").strip()
    if persisted_session:
        return persisted_session, channel, chat_id

    if job.payload.deliver and job.payload.to:
        return f"{channel}:{chat_id}", channel, chat_id

    return f"cron:{job.id}", channel, chat_id
