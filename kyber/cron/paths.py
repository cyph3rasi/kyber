"""Shared cron store path helpers."""

from __future__ import annotations

import json
from pathlib import Path

from kyber.config.loader import get_data_dir

LEGACY_CRON_STORE_PATH = Path.home() / ".kyber" / "cron_store.json"


def get_cron_store_path() -> Path:
    """Return the canonical cron store path and migrate legacy data if needed."""
    store_path = get_data_dir() / "cron" / "jobs.json"
    migrate_legacy_cron_store(store_path)
    return store_path


def migrate_legacy_cron_store(store_path: Path | None = None) -> Path:
    """Best-effort migration from ~/.kyber/cron_store.json to cron/jobs.json."""
    target = store_path or (get_data_dir() / "cron" / "jobs.json")
    legacy = LEGACY_CRON_STORE_PATH
    if not legacy.exists():
        return target

    try:
        legacy_data = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception:
        return target

    legacy_jobs = legacy_data.get("jobs")
    if not isinstance(legacy_jobs, list) or not legacy_jobs:
        return target

    target.parent.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        target.write_text(json.dumps(legacy_data, indent=2), encoding="utf-8")
        _archive_legacy_store(legacy)
        return target

    try:
        current_data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return target

    current_jobs = current_data.get("jobs")
    if not isinstance(current_jobs, list):
        current_jobs = []

    existing_ids = {
        j.get("id")
        for j in current_jobs
        if isinstance(j, dict) and isinstance(j.get("id"), str)
    }

    appended = False
    for job in legacy_jobs:
        if not isinstance(job, dict):
            continue
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id or job_id in existing_ids:
            continue
        current_jobs.append(job)
        existing_ids.add(job_id)
        appended = True

    if appended:
        current_data["jobs"] = current_jobs
        target.write_text(json.dumps(current_data, indent=2), encoding="utf-8")

    # Migration is one-time. Archive legacy so deletes don't get resurrected.
    _archive_legacy_store(legacy)
    return target


def _archive_legacy_store(legacy: Path) -> None:
    """Best-effort archive of legacy cron store after successful migration."""
    try:
        archived = legacy.with_suffix(legacy.suffix + ".migrated")
        if archived.exists():
            legacy.unlink()
            return
        legacy.replace(archived)
    except Exception:
        # Non-fatal: if this fails, canonical path still works.
        pass
