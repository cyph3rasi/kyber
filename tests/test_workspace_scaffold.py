from __future__ import annotations

from kyber.cli.commands import _create_workspace_templates


def test_create_workspace_templates_creates_expected_files(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    _create_workspace_templates(workspace)

    assert (workspace / "AGENTS.md").exists()
    assert (workspace / "SOUL.md").exists()
    assert (workspace / "USER.md").exists()
    assert (workspace / "IDENTITY.md").exists()
    assert (workspace / "TOOLS.md").exists()
    assert (workspace / "HEARTBEAT.md").exists()
    assert (workspace / "memory").is_dir()
    assert (workspace / "memory" / "MEMORY.md").exists()
    assert (workspace / "skills").is_dir()
    assert (workspace / "skills" / "README.md").exists()


def test_create_workspace_templates_updates_legacy_skills_readme(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skills_readme = skills_dir / "README.md"
    skills_readme.write_text(
        "# Skills\n\n"
        "Drop custom skills here as folders containing a `SKILL.md`.\n\n"
        "Example:\n"
        "- `skills/my-skill/SKILL.md`\n\n"
        "Kyber also supports managed skills in `~/.kyber/skills/<skill>/SKILL.md`.\n"
    )

    _create_workspace_templates(workspace)

    updated = skills_readme.read_text()
    assert "This workspace is the canonical skills location." in updated
    assert "~/.kyber/skills" not in updated
