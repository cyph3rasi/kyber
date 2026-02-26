"""Todo tool for task management.

Provides an in-memory task list the agent uses to decompose complex tasks,
track progress, and maintain focus across long conversations.
"""

import json
from typing import Any

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry


VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


class TodoStore:
    """
    In-memory todo list. One instance per session.

    Items are ordered -- list position is priority. Each item has:
      - id: unique string identifier (agent-chosen)
      - content: task description
      - status: pending | in_progress | completed | cancelled
    """

    def __init__(self):
        self._items: list[dict[str, str]] = []

    def write(self, todos: list[dict[str, Any]], merge: bool = False) -> list[dict[str, str]]:
        """
        Write todos. Returns the full current list after writing.

        Args:
            todos: list of {id, content, status} dicts
            merge: if False, replace the entire list. If True, update
                   existing items by id and append new ones.
        """
        if not merge:
            self._items = [self._validate(t) for t in todos]
        else:
            existing = {item["id"]: item for item in self._items}
            for t in todos:
                item_id = str(t.get("id", "")).strip()
                if not item_id:
                    continue

                if item_id in existing:
                    if "content" in t and t["content"]:
                        existing[item_id]["content"] = str(t["content"]).strip()
                    if "status" in t and t["status"]:
                        status = str(t["status"]).strip().lower()
                        if status in VALID_STATUSES:
                            existing[item_id]["status"] = status
                else:
                    validated = self._validate(t)
                    existing[validated["id"]] = validated
                    self._items.append(validated)

            seen = set()
            rebuilt = []
            for item in self._items:
                current = existing.get(item["id"], item)
                if current["id"] not in seen:
                    rebuilt.append(current)
                    seen.add(current["id"])
            self._items = rebuilt
        return self.read()

    def read(self) -> list[dict[str, str]]:
        """Return a copy of the current list."""
        return [item.copy() for item in self._items]

    def has_items(self) -> bool:
        """Check if there are any items in the list."""
        return len(self._items) > 0

    @staticmethod
    def _validate(item: dict[str, Any]) -> dict[str, str]:
        """Validate and normalize a todo item."""
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            item_id = "?"

        content = str(item.get("content", "")).strip()
        if not content:
            content = "(no description)"

        status = str(item.get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"

        return {"id": item_id, "content": content, "status": status}


# Session-level store (injected via kwargs)
_todo_stores: dict[str, TodoStore] = {}


def get_todo_store(session_id: str) -> TodoStore:
    """Get or create a TodoStore for a session."""
    if session_id not in _todo_stores:
        _todo_stores[session_id] = TodoStore()
    return _todo_stores[session_id]


class TodoTool(Tool):
    """Manage task list for the current session."""

    @property
    def name(self) -> str:
        return "todo"

    @property
    def description(self) -> str:
        return (
            "Manage your task list for the current session. Use for complex tasks "
            "with 3+ steps or when the user provides multiple tasks. "
            "Call with no parameters to read the current list.\n\n"
            "Writing:\n"
            "- Provide 'todos' array to create/update items\n"
            "- merge=false (default): replace the entire list with a fresh plan\n"
            "- merge=true: update existing items by id, add any new ones\n\n"
            "Each item: {id: string, content: string, status: pending|in_progress|completed|cancelled}\n"
            "List order is priority. Only ONE item in_progress at a time.\n"
            "Mark items completed immediately when done. If something fails, "
            "cancel it and add a revised item.\n\n"
            "Always returns the full current list."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "Task items to write. Omit to read current list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Unique item identifier"},
                            "content": {"type": "string", "description": "Task description"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                                "description": "Current status"
                            }
                        },
                        "required": ["id", "content", "status"]
                    }
                },
                "merge": {
                    "type": "boolean",
                    "description": "true: update existing items by id, add new ones. false (default): replace the entire list.",
                    "default": False
                }
            },
            "required": []
        }

    @property
    def toolset(self) -> str:
        return "productivity"

    async def execute(self, todos: list[dict[str, Any]] | None = None, merge: bool = False, **kwargs) -> str:
        session_id = kwargs.get("session_id", "default")
        store = get_todo_store(session_id)

        if todos is not None:
            items = store.write(todos, merge)
        else:
            items = store.read()

        pending = sum(1 for i in items if i["status"] == "pending")
        in_progress = sum(1 for i in items if i["status"] == "in_progress")
        completed = sum(1 for i in items if i["status"] == "completed")
        cancelled = sum(1 for i in items if i["status"] == "cancelled")

        return json.dumps({
            "todos": items,
            "summary": {
                "total": len(items),
                "pending": pending,
                "in_progress": in_progress,
                "completed": completed,
                "cancelled": cancelled,
            },
        }, ensure_ascii=False)


# Self-register
registry.register(TodoTool())
