from __future__ import annotations

import json
from pathlib import Path

from kyber.cron import paths


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_migrate_legacy_copies_when_target_missing(tmp_path, monkeypatch) -> None:
    legacy = tmp_path / "legacy" / "cron_store.json"
    target = tmp_path / "data" / "cron" / "jobs.json"

    _write_json(
        legacy,
        {
            "version": 1,
            "jobs": [{"id": "legacy-job", "name": "Legacy Job"}],
        },
    )
    monkeypatch.setattr(paths, "LEGACY_CRON_STORE_PATH", legacy)

    out = paths.migrate_legacy_cron_store(target)
    assert out == target
    assert target.exists()

    data = json.loads(target.read_text(encoding="utf-8"))
    assert [j["id"] for j in data["jobs"]] == ["legacy-job"]


def test_migrate_legacy_merges_only_missing_ids(tmp_path, monkeypatch) -> None:
    legacy = tmp_path / "legacy" / "cron_store.json"
    target = tmp_path / "data" / "cron" / "jobs.json"

    _write_json(
        legacy,
        {
            "version": 1,
            "jobs": [
                {"id": "job-a", "name": "A (legacy)"},
                {"id": "job-b", "name": "B (legacy)"},
            ],
        },
    )
    _write_json(
        target,
        {
            "version": 1,
            "jobs": [
                {"id": "job-a", "name": "A (current)"},
                {"id": "job-c", "name": "C (current)"},
            ],
        },
    )
    monkeypatch.setattr(paths, "LEGACY_CRON_STORE_PATH", legacy)

    paths.migrate_legacy_cron_store(target)
    data = json.loads(target.read_text(encoding="utf-8"))
    ids = [j.get("id") for j in data.get("jobs", [])]
    assert set(ids) == {"job-a", "job-b", "job-c"}
    assert len(ids) == 3


def test_get_cron_store_path_uses_canonical_location_and_migrates(tmp_path, monkeypatch) -> None:
    legacy = tmp_path / "legacy" / "cron_store.json"
    data_dir = tmp_path / "kyber-data"
    expected = data_dir / "cron" / "jobs.json"

    _write_json(
        legacy,
        {
            "version": 1,
            "jobs": [{"id": "legacy-job", "name": "Legacy Job"}],
        },
    )
    monkeypatch.setattr(paths, "LEGACY_CRON_STORE_PATH", legacy)
    monkeypatch.setattr(paths, "get_data_dir", lambda: data_dir)

    out = paths.get_cron_store_path()
    assert out == expected
    assert expected.exists()


def test_migration_is_one_time_and_does_not_resurrect_deleted_jobs(tmp_path, monkeypatch) -> None:
    legacy = tmp_path / "legacy" / "cron_store.json"
    data_dir = tmp_path / "kyber-data"
    target = data_dir / "cron" / "jobs.json"

    _write_json(
        legacy,
        {
            "version": 1,
            "jobs": [{"id": "job-a", "name": "A"}],
        },
    )
    monkeypatch.setattr(paths, "LEGACY_CRON_STORE_PATH", legacy)
    monkeypatch.setattr(paths, "get_data_dir", lambda: data_dir)

    # First call migrates and archives legacy.
    out = paths.get_cron_store_path()
    assert out == target
    assert target.exists()
    assert not legacy.exists()
    assert legacy.with_suffix(".json.migrated").exists()

    # Simulate deleting from canonical store.
    _write_json(target, {"version": 1, "jobs": []})

    # Subsequent calls must not re-import deleted jobs.
    paths.get_cron_store_path()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["jobs"] == []
