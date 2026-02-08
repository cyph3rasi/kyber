"""Tests for the task registry."""

from kyber.agent.task_registry import TaskRegistry, TaskStatus


def test_create_task_generates_reference() -> None:
    registry = TaskRegistry()
    task = registry.create(
        description="Test task",
        label="Test",
    )
    
    assert task.reference.startswith("⚡")
    assert len(task.reference) == 9  # ⚡ + 8 hex chars
    assert task.status == TaskStatus.QUEUED


def test_get_by_ref_with_emoji() -> None:
    registry = TaskRegistry()
    task = registry.create(description="Test", label="Test")
    
    # Should find by full reference
    found = registry.get_by_ref(task.reference)
    assert found is not None
    assert found.id == task.id
    
    # Should find by bare hex
    bare_ref = task.reference[1:]  # Remove ⚡
    found = registry.get_by_ref(bare_ref)
    assert found is not None
    assert found.id == task.id


def test_mark_completed_generates_completion_reference() -> None:
    registry = TaskRegistry()
    task = registry.create(description="Test", label="Test")
    
    registry.mark_started(task.id)
    registry.mark_completed(task.id, "Done!")
    
    updated = registry.get(task.id)
    assert updated is not None
    assert updated.status == TaskStatus.COMPLETED
    assert updated.completion_reference is not None
    assert updated.completion_reference.startswith("✅")
    assert updated.result == "Done!"


def test_get_active_tasks() -> None:
    registry = TaskRegistry()
    
    task1 = registry.create(description="Task 1", label="T1")
    task2 = registry.create(description="Task 2", label="T2")
    
    registry.mark_started(task1.id)
    registry.mark_completed(task1.id, "Done")
    registry.mark_started(task2.id)
    
    active = registry.get_active_tasks()
    assert len(active) == 1
    assert active[0].id == task2.id


def test_context_summary() -> None:
    registry = TaskRegistry()
    
    # Empty registry
    summary = registry.get_context_summary()
    assert "No active" in summary
    
    # With active task
    task = registry.create(description="Test", label="Test Task")
    registry.mark_started(task.id)
    
    summary = registry.get_context_summary()
    assert "Active tasks" in summary
    assert "Test Task" in summary


def test_history_includes_new_tasks_when_persistence_enabled(tmp_path) -> None:
    history_path = tmp_path / "history.jsonl"
    registry = TaskRegistry(history_path=history_path)

    task = registry.create(description="Test", label="History Task")
    registry.mark_started(task.id)
    registry.mark_completed(task.id, "Done!")

    hist = registry.get_history(limit=10)
    assert len(hist) >= 1
    assert any(t.id == task.id and t.status == TaskStatus.COMPLETED for t in hist)
