"""Cron job management tools."""

import re
from datetime import datetime
from typing import Any

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry


def _get_cron_service():
    """Get the cron service instance."""
    from kyber.cron.paths import get_cron_store_path
    from kyber.cron.service import CronService
    store_path = get_cron_store_path()
    return CronService(store_path)


def _parse_schedule(schedule_str: str):
    """Parse a schedule string into a CronSchedule.
    
    Supports:
    - "30m", "2h", "1d" (one-shot delays)
    - "every 30m", "every 2h" (recurring intervals)
    - "0 9 * * *" (cron expressions)
    - ISO timestamps: "2026-02-03T14:00:00"
    """
    from kyber.cron.types import CronSchedule
    
    s = schedule_str.strip().lower()
    
    # Recurring interval: "every 30m", "every 2h"
    every_match = re.match(r"^every\s+(\d+)([mhd])$", s)
    if every_match:
        amount = int(every_match.group(1))
        unit = every_match.group(2)
        multipliers = {"m": 60000, "h": 3600000, "d": 86400000}
        return CronSchedule(kind="every", every_ms=amount * multipliers[unit])
    
    # One-shot delay: "30m", "2h", "1d"
    delay_match = re.match(r"^(\d+)([mhd])$", s)
    if delay_match:
        amount = int(delay_match.group(1))
        unit = delay_match.group(2)
        multipliers = {"m": 60000, "h": 3600000, "d": 86400000}
        now_ms = int(datetime.now().timestamp() * 1000)
        return CronSchedule(kind="at", at_ms=now_ms + (amount * multipliers[unit]))
    
    # ISO timestamp: "2026-02-03T14:00:00"
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", schedule_str)
    if iso_match:
        try:
            dt = datetime.fromisoformat(iso_match.group(1))
            return CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
        except ValueError:
            pass
    
    # Cron expression: "0 9 * * *"
    cron_pattern = r"^[\d\*\-,/]+$"
    if re.match(cron_pattern, schedule_str.replace(" ", "")):
        return CronSchedule(kind="cron", expr=schedule_str)
    
    raise ValueError(f"Invalid schedule format: {schedule_str}")


def _format_time(ms: int | None) -> str:
    """Format milliseconds timestamp to readable string."""
    if not ms:
        return "N/A"
    dt = datetime.fromtimestamp(ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class ListCronjobsTool(Tool):
    """List all scheduled cronjobs."""

    toolset = "cron"

    @property
    def name(self) -> str:
        return "list_cronjobs"

    @property
    def description(self) -> str:
        return (
            "List all scheduled cronjobs with their IDs, schedules, and status.\n\n"
            "Use this to:\n"
            "- See what jobs are currently scheduled\n"
            "- Find job IDs for removal with remove_cronjob\n"
            "- Check job status and next run times\n\n"
            "Returns job_id, name, schedule, repeat status, next/last run times."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "include_disabled": {
                    "type": "boolean",
                    "description": "Include disabled/completed jobs in the list (default: false)",
                },
            },
            "required": [],
        }

    async def execute(self, include_disabled: bool = False, **kwargs) -> str:
        import json
        
        service = _get_cron_service()
        jobs = service.list_jobs(include_disabled=include_disabled)
        
        result = []
        for job in jobs:
            schedule_str = ""
            if job.schedule.kind == "at":
                schedule_str = f"at {_format_time(job.schedule.at_ms)}"
            elif job.schedule.kind == "every":
                # Convert ms back to human-readable
                every_min = job.schedule.every_ms // 60000
                if every_min >= 60 and every_min % 60 == 0:
                    schedule_str = f"every {every_min // 60}h"
                else:
                    schedule_str = f"every {every_min}m"
            elif job.schedule.kind == "cron":
                schedule_str = f"cron: {job.schedule.expr}"
            
            result.append({
                "id": job.id,
                "name": job.name,
                "schedule": schedule_str,
                "enabled": job.enabled,
                "next_run": _format_time(job.state.next_run_at_ms),
                "last_run": _format_time(job.state.last_run_at_ms),
                "last_status": job.state.last_status,
            })
        
        return json.dumps({"jobs": result, "count": len(result)}, ensure_ascii=False)


class ScheduleCronjobTool(Tool):
    """Schedule an automated task to run on a schedule."""

    toolset = "cron"

    @property
    def name(self) -> str:
        return "schedule_cronjob"

    @property
    def description(self) -> str:
        return (
            "Schedule an automated task to run the agent on a schedule.\n\n"
            "Cron jobs run with the full kyber runtime (same tools, skills, and memory).\n"
            "When scheduled from chat, they also reuse that chat session context by default.\n\n"
            "SCHEDULE FORMATS:\n"
            "- One-shot: \"30m\", \"2h\", \"1d\" (runs once after delay)\n"
            "- Interval: \"every 30m\", \"every 2h\" (recurring)\n"
            "- Cron: \"0 9 * * *\" (cron expression for precise scheduling)\n"
            "- Timestamp: \"2026-02-03T14:00:00\" (specific date/time)\n\n"
            "REPEAT BEHAVIOR:\n"
            "- One-shot schedules: run once by default\n"
            "- Intervals/cron: run forever by default\n"
            "- Set repeat=N to run exactly N times then auto-delete\n\n"
            "DELIVERY OPTIONS (where output goes):\n"
            "- \"origin\": Back to current chat\n"
            "- \"local\": Save to local files only\n"
            "- \"telegram:123456\": Send to specific chat (if user provides ID)"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Instructions for what the scheduled run should do.",
                },
                "schedule": {
                    "type": "string",
                    "description": "When to run: '30m' (once in 30min), 'every 30m' (recurring), '0 9 * * *' (cron), or ISO timestamp",
                },
                "name": {
                    "type": "string",
                    "description": "Optional human-friendly name for the job",
                },
                "repeat": {
                    "type": "integer",
                    "description": "Number of times to run. Omit for default (once for one-shot, forever for recurring).",
                },
                "deliver": {
                    "type": "string",
                    "description": "Where to send output: 'origin' (back to this chat), 'local' (files only), or 'platform:chat_id' (e.g. 'discord:12345')",
                },
            },
            "required": ["prompt", "schedule"],
        }

    async def execute(
        self,
        prompt: str,
        schedule: str,
        name: str | None = None,
        repeat: int | None = None,
        deliver: str = "local",
        **kwargs
    ) -> str:
        import json
        
        service = _get_cron_service()

        source_session_key = str(
            kwargs.get("session_key") or kwargs.get("task_id") or ""
        ).strip() or None
        origin_channel = str(kwargs.get("context_channel") or "").strip()
        origin_chat_id = str(kwargs.get("context_chat_id") or "").strip()
        
        try:
            cron_schedule = _parse_schedule(schedule)
        except ValueError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        
        # For one-shot schedules, delete after run
        delete_after = cron_schedule.kind == "at"
        
        # Parse deliver target
        channel = None
        to = None
        should_deliver = deliver not in ("local",)
        
        if deliver.startswith("telegram:"):
            channel = "telegram"
            to = deliver.split(":", 1)[1]
        elif deliver.startswith("discord:"):
            channel = "discord"
            to = deliver.split(":", 1)[1]
        elif deliver == "origin":
            if origin_channel and origin_chat_id:
                channel = origin_channel
                to = origin_chat_id
            else:
                should_deliver = False
        elif deliver in ("telegram", "discord"):
            channel = deliver
            if origin_channel == deliver and origin_chat_id:
                to = origin_chat_id
        elif deliver not in ("local",):
            return json.dumps(
                {
                    "error": (
                        "Invalid deliver target. Use 'local', 'origin', "
                        "or 'platform:chat_id' (e.g. 'discord:12345')."
                    )
                },
                ensure_ascii=False,
            )

        if should_deliver and not to:
            return json.dumps(
                {
                    "error": (
                        "Delivery target requires a chat ID. "
                        "Use 'origin' or provide 'platform:chat_id'."
                    )
                },
                ensure_ascii=False,
            )
        
        job = service.add_job(
            name=name or "Agent Task",
            schedule=cron_schedule,
            message=prompt,
            deliver=should_deliver,
            channel=channel,
            to=to,
            session_key=source_session_key,
            delete_after_run=delete_after,
        )
        
        return json.dumps({
            "id": job.id,
            "name": job.name,
            "schedule": schedule,
            "next_run": _format_time(job.state.next_run_at_ms),
            "status": "scheduled",
        }, ensure_ascii=False)


class RemoveCronjobTool(Tool):
    """Remove a scheduled cronjob by its ID."""

    toolset = "cron"

    @property
    def name(self) -> str:
        return "remove_cronjob"

    @property
    def description(self) -> str:
        return (
            "Remove a scheduled cronjob by its ID.\n\n"
            "Use list_cronjobs first to find the job_id of the job you want to remove.\n"
            "Jobs that have completed their repeat count are auto-removed, but you can\n"
            "use this to cancel a job before it completes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The ID of the cronjob to remove (from list_cronjobs output)",
                },
            },
            "required": ["job_id"],
        }

    async def execute(self, job_id: str, **kwargs) -> str:
        import json
        
        service = _get_cron_service()
        removed = service.remove_job(job_id)
        
        if removed:
            return json.dumps({"status": "removed", "job_id": job_id}, ensure_ascii=False)
        else:
            return json.dumps({"error": f"Job {job_id} not found", "status": "not_found"}, ensure_ascii=False)


# Self-register
registry.register(ListCronjobsTool())
registry.register(ScheduleCronjobTool())
registry.register(RemoveCronjobTool())
