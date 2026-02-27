from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import kyber.agent.skills as skills_mod
from kyber.agent.tools.skills import SkillManageTool


def test_skill_manage_create_writes_workspace_skill(tmp_path, monkeypatch) -> None:
    managed_dir = tmp_path / "managed-skills"
    monkeypatch.setattr(skills_mod, "MANAGED_SKILLS_DIR", managed_dir)

    workspace = tmp_path / "workspace"
    agent_core = SimpleNamespace(workspace=workspace)
    tool = SkillManageTool()

    payload = asyncio.run(
        tool.execute(
            action="create",
            name="email-checker",
            content="# Email Checker\n",
            category="productivity",
            agent_core=agent_core,
        )
    )
    result = json.loads(payload)

    expected_dir = workspace / "skills" / "email-checker"
    assert result["status"] == "created"
    assert result["source"] == "workspace"
    assert result["path"] == str(expected_dir)
    assert (expected_dir / "SKILL.md").read_text(encoding="utf-8") == "# Email Checker\n"
    assert not (managed_dir / "email-checker").exists()
    assert not (managed_dir / "productivity" / "email-checker").exists()


def test_skill_manage_edit_prefers_workspace_over_managed(tmp_path, monkeypatch) -> None:
    managed_dir = tmp_path / "managed-skills"
    monkeypatch.setattr(skills_mod, "MANAGED_SKILLS_DIR", managed_dir)

    workspace_skill = tmp_path / "workspace" / "skills" / "dup-skill"
    managed_skill = managed_dir / "dup-skill"
    workspace_skill.mkdir(parents=True, exist_ok=True)
    managed_skill.mkdir(parents=True, exist_ok=True)
    (workspace_skill / "SKILL.md").write_text("workspace-old\n", encoding="utf-8")
    (managed_skill / "SKILL.md").write_text("managed-old\n", encoding="utf-8")

    tool = SkillManageTool()
    payload = asyncio.run(
        tool.execute(
            action="edit",
            name="dup-skill",
            content="workspace-new\n",
            agent_core=SimpleNamespace(workspace=tmp_path / "workspace"),
        )
    )
    result = json.loads(payload)

    assert result["status"] == "edited"
    assert result["source"] == "workspace"
    assert (workspace_skill / "SKILL.md").read_text(encoding="utf-8") == "workspace-new\n"
    assert (managed_skill / "SKILL.md").read_text(encoding="utf-8") == "managed-old\n"
