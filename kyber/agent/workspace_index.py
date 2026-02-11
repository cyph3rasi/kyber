"""
Workspace Index: Precomputed file tree + summaries so workers don't waste
turns on list_dir/read_file exploration.

The index is built once (lazily, on first task) and refreshed periodically.
Workers get the index injected into their system prompt so the LLM already
knows what files exist and what they contain — it can jump straight to the
actual work instead of spending 5-10 tool calls just orienting itself.
"""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

# Files/dirs to always skip
_IGNORE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".tox", "dist", "build",
    ".egg-info", ".eggs", ".cache", ".DS_Store",
}

_IGNORE_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".tar", ".gz", ".bz2",
    ".lock", ".map",
}

# Max file size to include a preview (skip large files)
_MAX_PREVIEW_BYTES = 8192
# Max total index size in chars (don't blow up the context window)
_MAX_INDEX_CHARS = 12000


class WorkspaceIndex:
    """
    Maintains a lightweight, LLM-friendly snapshot of the workspace.

    The index contains:
    - Full file tree (paths + sizes)
    - First ~20 lines of key files (py, md, toml, json, yaml, etc.)
    - File purposes inferred from names/paths

    This gets injected into the worker's system prompt so it can skip
    the "exploration phase" that typically eats 5-10 tool calls.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._index: str | None = None
        self._built_at: float = 0
        self._file_hash: str = ""
        # Refresh every 5 minutes max
        self._ttl = 300

    def get(self) -> str:
        """Get the current index, building it if stale or missing."""
        now = time.time()
        if self._index and (now - self._built_at) < self._ttl:
            return self._index

        new_hash = self._quick_hash()
        if self._index and new_hash == self._file_hash:
            self._built_at = now
            return self._index

        self._index = self._build()
        self._built_at = now
        self._file_hash = new_hash
        return self._index

    def invalidate(self) -> None:
        """Force a rebuild on next access."""
        self._index = None

    def _quick_hash(self) -> str:
        """Fast hash of top-level directory listing to detect changes."""
        try:
            entries = sorted(
                f"{p.name}:{p.stat().st_mtime:.0f}"
                for p in self.workspace.iterdir()
                if p.name not in _IGNORE_DIRS
            )
            return hashlib.md5("|".join(entries).encode()).hexdigest()
        except Exception:
            return ""

    def _build(self) -> str:
        """Build the workspace index."""
        logger.debug("Building workspace index")
        tree_lines: list[str] = []
        previews: list[str] = []
        total_chars = 0

        def _walk(directory: Path, prefix: str = "", depth: int = 0):
            nonlocal total_chars
            if depth > 5 or total_chars > _MAX_INDEX_CHARS:
                return

            try:
                entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            except PermissionError:
                return

            for entry in entries:
                if entry.name in _IGNORE_DIRS or entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    tree_lines.append(f"{prefix}{entry.name}/")
                    _walk(entry, prefix + "  ", depth + 1)
                elif entry.is_file():
                    if entry.suffix in _IGNORE_EXTENSIONS:
                        continue
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        continue
                    size_str = _human_size(size)
                    rel = entry.relative_to(self.workspace)
                    tree_lines.append(f"{prefix}{entry.name}  ({size_str})")

                    # Include preview for important files
                    if total_chars < _MAX_INDEX_CHARS and self._should_preview(entry):
                        preview = self._get_preview(entry)
                        if preview:
                            previews.append(f"### {rel}\n```\n{preview}\n```")
                            total_chars += len(preview) + 50

        _walk(self.workspace)

        parts = ["## File Tree\n```"]
        parts.extend(tree_lines)
        parts.append("```")

        if previews:
            parts.append("\n## Key File Previews")
            parts.extend(previews)

        result = "\n".join(parts)
        logger.debug(f"Workspace index built: {len(tree_lines)} files, {len(result)} chars")
        return result

    def _should_preview(self, path: Path) -> bool:
        """Decide if a file is worth previewing."""
        name = path.name.lower()
        # Always preview these
        if name in {
            "readme.md", "pyproject.toml", "package.json", "cargo.toml",
            "makefile", "dockerfile", "docker-compose.yml",
            "requirements.txt", "setup.py", "setup.cfg",
        }:
            return True
        # Preview entry points and configs
        if name in {"main.py", "app.py", "index.ts", "index.js", "config.py", "settings.py"}:
            return True
        # Preview __init__.py files (they often define the module's API)
        if name == "__init__.py":
            try:
                return path.stat().st_size > 50  # skip empty inits
            except OSError:
                return False
        return False

    def _get_preview(self, path: Path) -> str | None:
        """Get first ~20 lines of a file."""
        try:
            if path.stat().st_size > _MAX_PREVIEW_BYTES:
                return None
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()[:20]
            return "\n".join(lines)
        except Exception:
            return None


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"
