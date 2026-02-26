from __future__ import annotations

import json
from pathlib import Path

from kyber.skillhub import manager as m


def test_reconcile_manifest_prunes_deleted_skills(tmp_path: Path, monkeypatch) -> None:
    managed = tmp_path / "skills"
    managed.mkdir(parents=True, exist_ok=True)

    # Patch managed skills dir for this test.
    monkeypatch.setattr(m, "MANAGED_SKILLS_DIR", managed)

    # Create two skills on disk.
    (managed / "a").mkdir()
    (managed / "a" / "SKILL.md").write_text("# A", encoding="utf-8")
    (managed / "b").mkdir()
    (managed / "b" / "SKILL.md").write_text("# B", encoding="utf-8")

    # Manifest claims a, b, and missing c.
    manifest_path = managed / ".kyber-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "installed": {
                    "pkg": {
                        "source": "x/y",
                        "skills": ["a", "b", "c"],
                        "updated_at": "t",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    out = m.reconcile_manifest()
    rec = out["installed"]["pkg"]
    assert rec["skills"] == ["a", "b"]

    # Delete b and reconcile again.
    (managed / "b" / "SKILL.md").unlink()
    (managed / "b").rmdir()
    # Directory removal above fails if not empty; ensure empty:
    # (note: if rmdir fails, test will fail)
    out2 = m.reconcile_manifest()
    rec2 = out2["installed"]["pkg"]
    assert rec2["skills"] == ["a"]
