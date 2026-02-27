"""Memory tool for persistent curated memory.

Provides bounded, file-backed memory that persists across sessions. Two stores:
  - memory/MEMORY.md: agent's personal notes and observations
  - USER.md: what the agent knows about the user (single profile file in workspace root)
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry
from kyber.utils.helpers import get_workspace_path


ENTRY_DELIMITER = "\n§\n"
USER_MEMORY_BLOCK_START = "<!-- KYBER_USER_MEMORY_START -->"
USER_MEMORY_BLOCK_END = "<!-- KYBER_USER_MEMORY_END -->"


class MemoryStore:
    """
    Bounded curated memory with file persistence.
    """

    def __init__(
        self,
        memory_dir: Path,
        user_profile_file: Path,
        memory_char_limit: int = 2200,
        user_char_limit: int = 1375,
    ):
        self.memory_dir = memory_dir
        self.user_profile_file = user_profile_file
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self._system_prompt_snapshot: dict[str, str] = {"memory": "", "user": ""}

    def load_from_disk(self):
        """Load entries from MEMORY.md and USER.md."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(self.memory_dir / "MEMORY.md")
        self.user_entries = self._read_user_entries(self.user_profile_file)

        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        if target == "memory":
            self._write_file(self.memory_dir / "MEMORY.md", self.memory_entries)
        elif target == "user":
            self._write_user_entries(self.user_profile_file, self.user_entries)

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
    def _read_user_entries(path: Path) -> list[str]:
        """Read managed user-memory entries from workspace USER.md."""
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        start = raw.find(USER_MEMORY_BLOCK_START)
        end = raw.find(USER_MEMORY_BLOCK_END)
        if start != -1 and end != -1 and end > start:
            block = raw[start + len(USER_MEMORY_BLOCK_START):end].strip()
            if not block:
                return []
            entries = [e.strip() for e in block.split("§")]
            return [e for e in entries if e]

        # Backward-compatibility path: if an older formatter wrote delimiter-only
        # content to USER.md, preserve it as managed entries.
        if "§" in raw:
            entries = [e.strip() for e in raw.split("§")]
            return [e for e in entries if e]

        return []

    @staticmethod
    def _write_file(path: Path, entries: list[str]):
        """Write entries to a memory file using atomic temp-file + rename."""
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        path.parent.mkdir(parents=True, exist_ok=True)
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

    @staticmethod
    def _write_user_entries(path: Path, entries: list[str]) -> None:
        """Write managed user-memory entries into the workspace USER.md file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, IOError):
                existing = ""

        start = existing.find(USER_MEMORY_BLOCK_START)
        end = existing.find(USER_MEMORY_BLOCK_END)
        payload = ENTRY_DELIMITER.join(entries) if entries else ""

        block = (
            "## Kyber User Memory\n\n"
            f"{USER_MEMORY_BLOCK_START}\n"
            f"{payload}\n"
            f"{USER_MEMORY_BLOCK_END}\n"
        )

        if start != -1 and end != -1 and end > start:
            section_start = existing.rfind("## Kyber User Memory", 0, start)
            if section_start == -1:
                section_start = start
            end_idx = end + len(USER_MEMORY_BLOCK_END)
            new_content = existing[:section_start].rstrip() + "\n\n" + block
            if end_idx < len(existing):
                new_content += "\n" + existing[end_idx:].lstrip()
        else:
            base = existing.rstrip()
            if base:
                new_content = f"{base}\n\n{block}"
            else:
                new_content = (
                    "# User\n\n"
                    "Information about the user goes here.\n\n"
                    f"{block}"
                )

        MemoryStore._write_file(path, [new_content.rstrip() + "\n"])


# Global store instance
_memory_store: MemoryStore | None = None
_memory_store_key: tuple[Path, Path] | None = None


def _resolve_memory_paths(agent_core: Any | None = None) -> tuple[Path, Path]:
    """Resolve workspace memory dir and user profile file paths."""
    workspace = getattr(agent_core, "workspace", None) if agent_core else None
    if not isinstance(workspace, Path):
        workspace = get_workspace_path()
    return workspace / "memory", workspace / "USER.md"


def get_memory_store(memory_dir: Path, user_profile_file: Path) -> MemoryStore:
    """Get or create the global MemoryStore for the given workspace memory paths."""
    global _memory_store, _memory_store_key
    resolved_dir = memory_dir.expanduser().resolve(strict=False)
    resolved_user = user_profile_file.expanduser().resolve(strict=False)
    key = (resolved_dir, resolved_user)
    if _memory_store is None or _memory_store_key != key:
        _memory_store = MemoryStore(
            memory_dir=resolved_dir,
            user_profile_file=resolved_user,
        )
        _memory_store.load_from_disk()
        _memory_store_key = key
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
            "- 'user': who the user is -- writes to workspace USER.md (single profile file)\n"
            "- 'memory': your notes -- writes to workspace memory/MEMORY.md\n\n"
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
                    "description": "Which memory store: 'memory' for personal notes (memory/MEMORY.md), 'user' for user profile (USER.md)."
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
        memory_dir, user_file = _resolve_memory_paths(kwargs.get("agent_core"))
        store = get_memory_store(memory_dir, user_file)

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
