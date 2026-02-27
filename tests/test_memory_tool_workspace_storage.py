from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import kyber.agent.tools.memory as memory_tool


def test_resolve_memory_paths_use_agent_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    agent_core = SimpleNamespace(workspace=workspace)

    memory_dir, user_file = memory_tool._resolve_memory_paths(agent_core)

    assert memory_dir == workspace / "memory"
    assert user_file == workspace / "USER.md"


def test_memory_tool_writes_into_workspace_memory(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    agent_core = SimpleNamespace(workspace=workspace)
    monkeypatch.setattr(memory_tool, "_memory_store", None)
    monkeypatch.setattr(memory_tool, "_memory_store_key", None)

    tool = memory_tool.MemoryTool()
    raw = asyncio.run(
        tool.execute(
            action="add",
            target="memory",
            content="remember this",
            agent_core=agent_core,
        )
    )
    result = json.loads(raw)

    assert result["success"] is True
    memory_file = workspace / "memory" / "MEMORY.md"
    assert memory_file.exists()
    assert "remember this" in memory_file.read_text(encoding="utf-8")


def test_memory_tool_user_target_writes_workspace_user_md_only(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "USER.md").write_text("# User\n\nExisting profile content.\n", encoding="utf-8")
    agent_core = SimpleNamespace(workspace=workspace)
    monkeypatch.setattr(memory_tool, "_memory_store", None)
    monkeypatch.setattr(memory_tool, "_memory_store_key", None)

    tool = memory_tool.MemoryTool()
    raw = asyncio.run(
        tool.execute(
            action="add",
            target="user",
            content="Prefers concise responses.",
            agent_core=agent_core,
        )
    )
    result = json.loads(raw)

    assert result["success"] is True
    user_file = workspace / "USER.md"
    assert user_file.exists()
    user_text = user_file.read_text(encoding="utf-8")
    assert "Prefers concise responses." in user_text
    assert memory_tool.USER_MEMORY_BLOCK_START in user_text
    assert not (workspace / "memory" / "USER.md").exists()


def test_get_memory_store_reinitializes_for_new_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_tool, "_memory_store", None)
    monkeypatch.setattr(memory_tool, "_memory_store_key", None)

    first = tmp_path / "one"
    second = tmp_path / "two"

    store_one = memory_tool.get_memory_store(first / "memory", first / "USER.md")
    store_two = memory_tool.get_memory_store(second / "memory", second / "USER.md")

    assert store_one is not store_two
    assert memory_tool._memory_store_key == (
        (second / "memory").resolve(strict=False),
        (second / "USER.md").resolve(strict=False),
    )
