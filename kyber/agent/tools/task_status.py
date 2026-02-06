"""Task status tool for checking subagent progress."""

from typing import Any, TYPE_CHECKING

from kyber.agent.tools.base import Tool

if TYPE_CHECKING:
    from kyber.agent.subagent import SubagentManager


class TaskStatusTool(Tool):
    """
    Instant-lookup tool for checking the status of background subagent tasks.

    This is a lightweight tool that reads from an in-memory tracker — no LLM
    calls or heavy work involved.  The main agent should use this whenever the
    user asks about the progress of a running task.
    """

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager

    @property
    def name(self) -> str:
        return "task_status"

    @property
    def description(self) -> str:
        return (
            "Check the status and progress of background subagent tasks. "
            "Returns live progress including current step, elapsed time, and "
            "recent actions. Use this when the user asks about the status of "
            "a running task. Call with no arguments to see all tasks, or pass "
            "a task_id to check a specific one."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Optional: specific task ID to check. Omit to see all tasks.",
                },
            },
            "required": [],
        }

    async def execute(self, task_id: str | None = None, **kwargs: Any) -> str:
        if task_id:
            return self._manager.get_task_status(task_id)
        return self._manager.get_all_status()
