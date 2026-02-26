"""Memory tool for persistent curated memory.

Provides bounded, file-backed memory that persists across sessions. Two stores:
  - MEMORY.md: agent's personal notes and observations
  - USER.md: what the agent knows about the user
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry


# Where memory files live
MEMORY_DIR = Path.home() / ".kyber" / "memories"
ENTRY_DELIMITER = "\n§\n"


class MemoryStore:
    """
    Bounded curated memory with file persistence.
    """

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375):
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self._system_prompt_snapshot: dict[str, str] = {"memory": "", "user": ""}

    def load_from_disk(self):
        """Load entries from MEMORY.md and USER.md."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(MEMORY_DIR / "MEMORY.md")
        self.user_entries = self._read_file(MEMORY_DIR / "USER.md")

        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

        if target == "memory":
            self._write_file(MEMORY_DIR / "MEMORY.md", self.memory_entries)
        elif target == "user":
            self._write_file(MEMORY_DIR / "USER.md", self.user_entries)

    def _entries_for(self, target: str) -> list[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: list[str]):
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit

    def add(self, target: str, content: str) -> dict[str, Any]:
        """Append a new entry."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        entries = self._entries_for(target)
        limit = self._char_limit(target)

        if content in entries:
            return self._success_response(target, "Entry already exists (no duplicate added).")

        new_entries = entries + [content]
        new_total = len(ENTRY_DELIMITER.join(new_entries))

        if new_total > limit:
            current = self._char_count(target)
            return {
                "success": False,
                "error": (
                    f"Memory at {current:,}/{limit:,} chars. "
                    f"Adding this entry ({len(content)} chars) would exceed the limit. "
                    f"Replace or remove existing entries first."
                ),
                "current_entries": entries,
                "usage": f"{current:,}/{limit:,}",
            }

        entries.append(content)
        self._set_entries(target, entries)
        self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        entries = self._entries_for(target)
        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

        if len(matches) == 0:
            return {"success": False, "error": f"No entry matched '{old_text}'."}

        if len(matches) > 1:
            unique_texts = set(e for _, e in matches)
            if len(unique_texts) > 1:
                previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                return {
                    "success": False,
                    "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                    "matches": previews,
                }

        idx = matches[0][0]
        limit = self._char_limit(target)

        test_entries = entries.copy()
        test_entries[idx] = new_content
        new_total = len(ENTRY_DELIMITER.join(test_entries))

        if new_total > limit:
            return {
                "success": False,
                "error": (
                    f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                    f"Shorten the new content or remove other entries first."
                ),
            }

        entries[idx] = new_content
        self._set_entries(target, entries)
        self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        entries = self._entries_for(target)
        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

        if len(matches) == 0:
            return {"success": False, "error": f"No entry matched '{old_text}'."}

        if len(matches) > 1:
            unique_texts = set(e for _, e in matches)
            if len(unique_texts) > 1:
                previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                return {
                    "success": False,
                    "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                    "matches": previews,
                }

        idx = matches[0][0]
        entries.pop(idx)
        self._set_entries(target, entries)
        self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def format_for_system_prompt(self, target: str) -> str | None:
        """Return the frozen snapshot for system prompt injection."""
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    def _success_response(self, target: str, message: str = None) -> dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = int((current / limit) * 100) if limit > 0 else 0

        resp = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    def _render_block(self, target: str, entries: list[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = int((current / limit) * 100) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_file(path: Path) -> list[str]:
        """Read a memory file and split into entries."""
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        if not raw.strip():
            return []

        entries = [e.strip() for e in raw.split("§")]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: list[str]):
        """Write entries to a memory file using atomic temp-file + rename."""
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(path))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


# Global store instance
_memory_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    """Get or create the global MemoryStore."""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
        _memory_store.load_from_disk()
    return _memory_store


class MemoryTool(Tool):
    """Save important information to persistent memory."""

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return (
            "Save important information to persistent memory that survives across sessions. "
            "Your memory appears in your system prompt at session start -- it's how you "
            "remember things about the user and your environment between conversations.\n\n"
            "WHEN TO SAVE (do this proactively, don't wait to be asked):\n"
            "- User shares a preference, habit, or personal detail (name, role, timezone, coding style)\n"
            "- You discover something about the environment (OS, installed tools, project structure)\n"
            "- User corrects you or says 'remember this' / 'don't do that again'\n"
            "- You learn a convention, API quirk, or workflow specific to this user's setup\n"
            "- You completed something - log it like a diary entry\n\n"
            "TWO TARGETS:\n"
            "- 'user': who the user is -- name, role, preferences, communication style, pet peeves\n"
            "- 'memory': your notes -- environment facts, project conventions, tool quirks, lessons learned\n\n"
            "ACTIONS: add (new entry), replace (update existing -- old_text identifies it), "
            "remove (delete -- old_text identifies it).\n"
            "Capacity shown in system prompt. When >80%, consolidate entries before adding new ones.\n\n"
            "SKIP: trivial/obvious info, things easily re-discovered, raw data dumps."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "replace", "remove"],
                    "description": "The action to perform."
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "Which memory store: 'memory' for personal notes, 'user' for user profile."
                },
                "content": {
                    "type": "string",
                    "description": "The entry content. Required for 'add' and 'replace'."
                },
                "old_text": {
                    "type": "string",
                    "description": "Short unique substring identifying the entry to replace or remove."
                },
            },
            "required": ["action", "target"],
        }

    @property
    def toolset(self) -> str:
        return "memory"

    async def execute(
        self,
        action: str,
        target: str = "memory",
        content: str = None,
        old_text: str = None,
        **kwargs
    ) -> str:
        store = get_memory_store()

        if target not in ("memory", "user"):
            return json.dumps({"success": False, "error": f"Invalid target '{target}'. Use 'memory' or 'user'."}, ensure_ascii=False)

        if action == "add":
            if not content:
                return json.dumps({"success": False, "error": "Content is required for 'add' action."}, ensure_ascii=False)
            result = store.add(target, content)

        elif action == "replace":
            if not old_text:
                return json.dumps({"success": False, "error": "old_text is required for 'replace' action."}, ensure_ascii=False)
            if not content:
                return json.dumps({"success": False, "error": "content is required for 'replace' action."}, ensure_ascii=False)
            result = store.replace(target, old_text, content)

        elif action == "remove":
            if not old_text:
                return json.dumps({"success": False, "error": "old_text is required for 'remove' action."}, ensure_ascii=False)
            result = store.remove(target, old_text)

        else:
            return json.dumps({"success": False, "error": f"Unknown action '{action}'. Use: add, replace, remove"}, ensure_ascii=False)

        return json.dumps(result, ensure_ascii=False)


# Self-register
registry.register(MemoryTool())
