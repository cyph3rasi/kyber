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
