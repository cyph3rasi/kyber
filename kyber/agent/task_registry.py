"""
Task Registry: Single source of truth for all task state.

Every task has a reference (⚡ for started, ✅ for completed).
The registry tracks all state - the LLM sees this injected into context.

Notes:
- By default tasks are unlimited-length (no hard cap on tool iterations).
- Completed tasks can optionally be persisted for dashboard/history browsing.
"""

import secrets
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class TaskStatus(str, Enum):
    """Task lifecycle states."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def make_reference(prefix: str = "⚡") -> str:
    """Generate a short cryptographic reference token."""
    return f"{prefix}{secrets.token_hex(4)}"


@dataclass
class Task:
    """A tracked task with full lifecycle state."""
    id: str
    reference: str  # ⚡abc123 (start) or ✅def456 (completion)
    description: str
    label: str
    status: TaskStatus = TaskStatus.QUEUED
    
    # Origin info for routing responses
    origin_channel: str = "cli"
    origin_chat_id: str = "direct"
    
    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    
    # Progress tracking
    current_action: str = ""
    actions_completed: list[str] = field(default_factory=list)
    iteration: int = 0
    # None = unlimited
    max_iterations: int | None = None
    # Per-task toggle: allow 60s background progress pings for this task.
    progress_updates_enabled: bool = True
    
    # Results
    result: str | None = None
    error: str | None = None
    completion_reference: str | None = None  # ✅ reference on completion
    
    def to_progress_summary(self) -> str:
        """Short progress summary for context injection."""
        elapsed = datetime.now() - self.created_at
        mins, secs = divmod(int(elapsed.total_seconds()), 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        
        if self.status == TaskStatus.RUNNING:
            action_str = f", currently: {self.current_action}" if self.current_action else ""
            if self.max_iterations:
                step = f"step {self.iteration}/{self.max_iterations}"
            else:
                step = f"step {self.iteration}"
            return f"{self.reference}: \"{self.label}\" - {step}{action_str} ({time_str})"
        elif self.status == TaskStatus.QUEUED:
            return f"{self.reference}: \"{self.label}\" - queued ({time_str})"
        elif self.status == TaskStatus.COMPLETED:
            ref = self.completion_reference or self.reference
            return f"{ref}: \"{self.label}\" - completed ({time_str})"
        elif self.status == TaskStatus.FAILED:
            return f"{self.reference}: \"{self.label}\" - failed: {self.error or 'unknown'}"
        else:
            return f"{self.reference}: \"{self.label}\" - {self.status.value}"
    
    def to_full_summary(self) -> str:
        """Full summary for status requests."""
        lines = [
            f"Task: {self.label}",
            f"Reference: {self.reference}",
            f"Status: {self.status.value}",
        ]
        
        if self.completion_reference:
            lines.append(f"Completion: {self.completion_reference}")
        
        elapsed = (self.completed_at or datetime.now()) - self.created_at
        mins, secs = divmod(int(elapsed.total_seconds()), 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        lines.append(f"Time: {time_str}")
        
        if self.status == TaskStatus.RUNNING:
            if self.max_iterations:
                lines.append(f"Progress: step {self.iteration}/{self.max_iterations}")
            else:
                lines.append(f"Progress: step {self.iteration}")
            if self.current_action:
                lines.append(f"Currently: {self.current_action}")
            if self.actions_completed:
                recent = self.actions_completed[-5:]
                lines.append(f"Recent: {', '.join(recent)}")
        
        if self.result and self.status == TaskStatus.COMPLETED:
            preview = self.result[:300] + ("..." if len(self.result) > 300 else "")
            lines.append(f"Result: {preview}")
        
        if self.error:
            lines.append(f"Error: {self.error}")
        
        return "\n".join(lines)


class TaskRegistry:
    """
    Central registry for all tasks.

    This is the single source of truth. The LLM sees this state
    injected into every prompt, so it's always omniscient.
    """

    def __init__(self, history_path: Path | None = None):
        self._tasks: dict[str, Task] = {}
        self._ref_to_id: dict[str, str] = {}  # Map references to task IDs
        self._completed_cache: list[str] = []  # Recent completed task IDs
        self._max_completed_cache = 50
        self._history_path = history_path
        self._history: list[Task] = []
        self._load_history()

    def _load_history(self) -> None:
        if not self._history_path:
            return
        try:
            path = self._history_path.expanduser()
            if not path.exists():
                return
            for line in path.read_text(encoding="utf-8").splitlines()[-200:]:
                if not line.strip():
                    continue
                obj = json.loads(line)
                t = Task(
                    id=obj.get("id", secrets.token_hex(4)),
                    reference=obj.get("reference", make_reference("⚡")),
                    description=obj.get("description", ""),
                    label=obj.get("label", "Task"),
                    status=TaskStatus(obj.get("status", TaskStatus.COMPLETED.value)),
                    origin_channel=obj.get("origin_channel", "cli"),
                    origin_chat_id=obj.get("origin_chat_id", "direct"),
                    created_at=datetime.fromisoformat(obj["created_at"]) if obj.get("created_at") else datetime.now(),
                    started_at=datetime.fromisoformat(obj["started_at"]) if obj.get("started_at") else None,
                    completed_at=datetime.fromisoformat(obj["completed_at"]) if obj.get("completed_at") else None,
                    iteration=int(obj.get("iteration", 0) or 0),
                    max_iterations=obj.get("max_iterations"),
                    result=obj.get("result"),
                    error=obj.get("error"),
                    completion_reference=obj.get("completion_reference"),
                    progress_updates_enabled=bool(obj.get("progress_updates_enabled", True)),
                )
                self._history.append(t)
                # Map refs for lookup
                self._ref_to_id[t.reference] = t.id
                self._ref_to_id[t.reference[1:]] = t.id
                if t.completion_reference:
                    self._ref_to_id[t.completion_reference] = t.id
                    self._ref_to_id[t.completion_reference[1:]] = t.id
                # Store in tasks dict as completed history so status lookup works.
                self._tasks[t.id] = t
        except Exception:
            # Best-effort; ignore history load errors.
            return

    def _append_history(self, task: Task) -> None:
        # Always maintain in-memory history so dashboards/status checks work
        # even if the history file can't be written (or isn't configured).
        self._history.append(task)
        if len(self._history) > 500:
            self._history = self._history[-500:]

        if not self._history_path:
            return
        try:
            path = self._history_path.expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            obj = {
                "id": task.id,
                "reference": task.reference,
                "completion_reference": task.completion_reference,
                "description": task.description,
                "label": task.label,
                "status": task.status.value,
                "origin_channel": task.origin_channel,
                "origin_chat_id": task.origin_chat_id,
                "created_at": task.created_at.isoformat(),
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                "iteration": task.iteration,
                "max_iterations": task.max_iterations,
                "progress_updates_enabled": task.progress_updates_enabled,
                # Results may be large; keep them but cap at 200k chars.
                "result": (task.result[:200_000] if task.result else None),
                "error": task.error,
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=True) + "\n")
        except Exception:
            return

    def create(
        self,
        description: str,
        label: str,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        complexity: str | None = None,
    ) -> Task:
        """Create a new task and return it."""
        task_id = secrets.token_hex(4)
        reference = make_reference("⚡")

        task = Task(
            id=task_id,
            reference=reference,
            description=description,
            label=label,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            max_iterations=None,  # unlimited
        )

        self._tasks[task_id] = task
        self._ref_to_id[reference] = task_id
        self._ref_to_id[reference[1:]] = task_id  # Also map bare hex

        return task

    def find_active_duplicate(
        self,
        *,
        label: str,
        description: str,
        origin_channel: str,
        origin_chat_id: str,
        similarity_threshold: float = 0.9,
    ) -> Task | None:
        """Find a currently active task with a very similar label+description."""

        def _normalize(value: str) -> str:
            import re
            text = (value or "").lower().strip()
            text = re.sub(r"\s+", " ", text)
            return text[:3000]

        def _similar(a: str, b: str) -> float:
            from difflib import SequenceMatcher
            if not a or not b:
                return 0.0
            return SequenceMatcher(None, a, b).ratio()

        norm_label = _normalize(label)
        norm_desc = _normalize(description)
        if not norm_label:
            norm_label = "task"

        for task in self.get_active_tasks():
            if task.origin_channel != origin_channel or task.origin_chat_id != origin_chat_id:
                continue

            task_label = _normalize(task.label)
            if not task_label:
                task_label = "task"

            if _similar(norm_label, task_label) < 0.75:
                continue

            task_desc = _normalize(task.description)
            if norm_desc and task_desc:
                if _similar(norm_desc, task_desc) >= similarity_threshold:
                    return task
                if norm_desc in task_desc or task_desc in norm_desc:
                    if len(norm_desc) > 40 or len(task_desc) > 40:
                        return task

        return None

    def get(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        return self._tasks.get(task_id)

    def get_by_ref(self, ref: str) -> Task | None:
        """Get a task by reference (⚡abc123, ✅abc123, ❌abc123, or just abc123)."""
        clean_ref = ref.strip()
        if clean_ref and clean_ref[0] in "⚡✅❌":
            clean_ref = clean_ref[1:]

        task_id = self._ref_to_id.get(clean_ref) or self._ref_to_id.get(f"⚡{clean_ref}")
        if task_id:
            return self._tasks.get(task_id)
        return None

    def mark_started(self, task_id: str) -> None:
        """Mark a task as started."""
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now()

    def mark_completed(self, task_id: str, result: str) -> None:
        """Mark a task as completed with result."""
        task = self._tasks.get(task_id)
        if task:
            if task.status == TaskStatus.CANCELLED:
                return
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now()
            task.result = result
            task.completion_reference = make_reference("✅")
            task.current_action = ""

            self._ref_to_id[task.completion_reference] = task_id
            self._ref_to_id[task.completion_reference[1:]] = task_id

            self._completed_cache.append(task_id)
            if len(self._completed_cache) > self._max_completed_cache:
                self._completed_cache.pop(0)
            self._append_history(task)

    def mark_failed(self, task_id: str, error: str) -> None:
        """Mark a task as failed with error."""
        task = self._tasks.get(task_id)
        if task:
            if task.status == TaskStatus.CANCELLED:
                return
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.now()
            task.error = error
            task.completion_reference = make_reference("❌")
            task.current_action = ""

            self._ref_to_id[task.completion_reference] = task_id
            self._ref_to_id[task.completion_reference[1:]] = task_id
            self._append_history(task)

    def mark_cancelled(self, task_id: str, reason: str | None = None) -> None:
        """Mark a task as cancelled."""
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.CANCELLED
            task.completed_at = datetime.now()
            task.error = reason or task.error
            task.completion_reference = make_reference("❌")
            task.current_action = ""
            self._ref_to_id[task.completion_reference] = task_id
            self._ref_to_id[task.completion_reference[1:]] = task_id
            self._append_history(task)

    def update_progress(
        self,
        task_id: str,
        iteration: int | None = None,
        current_action: str | None = None,
        action_completed: str | None = None,
    ) -> None:
        """Update task progress."""
        task = self._tasks.get(task_id)
        if task:
            if iteration is not None:
                task.iteration = iteration
            if current_action is not None:
                task.current_action = current_action
            if action_completed:
                task.actions_completed.append(action_completed)

    def get_active_tasks(self) -> list[Task]:
        """Get all active (queued or running) tasks."""
        return [
            t for t in self._tasks.values()
            if t.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
        ]

    def get_recent_completed(self, limit: int = 5) -> list[Task]:
        """Get recently completed tasks."""
        recent_ids = self._completed_cache[-limit:]
        return [self._tasks[tid] for tid in recent_ids if tid in self._tasks]

    def get_history(self, limit: int = 50) -> list[Task]:
        """Return most recent completed tasks (including persisted history)."""
        # _history is append-only; tasks dict may contain history as well, but we preserve order here.
        if self._history:
            return list(self._history[-limit:])
        return self.get_recent_completed(limit)

    def has_active_tasks(self) -> bool:
        """Check if any tasks are active."""
        return any(
            t.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
            for t in self._tasks.values()
        )

    def get_context_summary(self) -> str:
        """
        Get a summary of current state for context injection.
        This is what the LLM sees in every prompt - makes it omniscient.
        """
        active = self.get_active_tasks()
        recent = self.get_recent_completed(3)

        if not active and not recent:
            return "No active or recent tasks."

        lines = []

        if active:
            lines.append("Active tasks:")
            for task in active:
                lines.append(f"  • {task.to_progress_summary()}")

        if recent:
            lines.append("Recently completed:")
            for task in recent:
                lines.append(f"  • {task.to_progress_summary()}")

        return "\n".join(lines)

    def get_status_for_ref(self, ref: str | None) -> str:
        """Get status for a specific reference or all tasks."""
        if ref:
            task = self.get_by_ref(ref)
            if task:
                return task.to_full_summary()
            # Common causes:
            # - The assistant hallucinated a Ref without spawning a task.
            # - The gateway restarted (registry is in-memory), losing task state.
            return (
                f"No task found with reference '{ref}'.\n"
                "This usually means no task was actually spawned for that Ref, "
                "or the gateway restarted and lost in-memory task state."
            )

        active = self.get_active_tasks()
        recent = self.get_recent_completed(5)

        if not active and not recent:
            return "No tasks to report on."

        parts = []
        if active:
            parts.append("=== Active ===")
            for task in active:
                parts.append(task.to_full_summary())
                parts.append("")

        if recent:
            parts.append("=== Recent ===")
            for task in recent:
                parts.append(task.to_full_summary())
                parts.append("")

        return "\n".join(parts).strip()
