"""Session management for conversation history."""

import json
import os
import tempfile
import threading
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from kyber.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    """
    A conversation session.
    
    Stores messages in JSONL format for easy reading and persistence.
    """
    
    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()
    
    def get_history(self, max_messages: int = 20) -> list[dict[str, Any]]:
        """
        Get message history for LLM context.
        
        Args:
            max_messages: Maximum messages to return.
        
        Returns:
            List of messages in LLM format.
        """
        # Get recent messages
        recent = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages
        
        # Convert to LLM format (just role and content)
        return [{"role": m["role"], "content": m["content"]} for m in recent]
    
    def clear(self) -> None:
        """Clear all messages in the session."""
        self.messages = []
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.
    
    Sessions are stored as JSONL files in the sessions directory.
    """
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(Path.home() / ".kyber" / "sessions")
        self._cache: dict[str, Session] = {}
        self._write_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
    
    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"
    
    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.
        
        Args:
            key: Session key (usually channel:chat_id).
        
        Returns:
            The session.
        """
        # Check cache
        if key in self._cache:
            return self._cache[key]
        
        # Try to load from disk
        session = self._load(key)
        if session is None:
            session = Session(key=key)
        
        self._cache[key] = session
        return session
    
    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        
        if not path.exists():
            return None
        
        try:
            messages = []
            metadata = {}
            created_at = None
            malformed_lines = 0
            
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_lines += 1
                        continue

                    if not isinstance(data, dict):
                        malformed_lines += 1
                        continue

                    if data.get("_type") == "metadata":
                        metadata_raw = data.get("metadata", {})
                        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
                        created_raw = data.get("created_at")
                        if created_raw:
                            try:
                                created_at = datetime.fromisoformat(str(created_raw))
                            except Exception:
                                created_at = None
                    else:
                        messages.append(data)

            if malformed_lines:
                logger.warning(
                    f"Session {key} contained {malformed_lines} malformed line(s); "
                    "loaded remaining valid entries."
                )
            
            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata
            )
        except Exception as e:
            logger.warning(f"Failed to load session {key}: {e}")
            return None
    
    def save(self, session: Session) -> None:
        """Save a session to disk (non-blocking via thread pool)."""
        self._cache[session.key] = session
        # Fire-and-forget: write to disk in a background thread so we don't
        # block the response path on file I/O.
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._write_session, session)
        except RuntimeError:
            # No running event loop (e.g. tests) — write synchronously.
            self._write_session(session)

    def _get_write_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._write_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._write_locks[key] = lock
            return lock

    def _write_session(self, session: Session) -> None:
        """Synchronous session write (called from executor)."""
        path = self._get_session_path(session.key)
        lock = self._get_write_lock(session.key)
        try:
            with lock:
                tmp_file = None
                tmp_path: str | None = None
                try:
                    tmp_file = tempfile.NamedTemporaryFile(
                        mode="w",
                        dir=str(path.parent),
                        prefix=f"{path.name}.",
                        suffix=".tmp",
                        delete=False,
                        encoding="utf-8",
                    )
                    tmp_path = tmp_file.name

                    metadata_line = {
                        "_type": "metadata",
                        "created_at": session.created_at.isoformat(),
                        "updated_at": session.updated_at.isoformat(),
                        "metadata": session.metadata,
                    }
                    tmp_file.write(json.dumps(metadata_line) + "\n")
                    for msg in session.messages:
                        tmp_file.write(json.dumps(msg) + "\n")
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())
                    tmp_file.close()
                    os.replace(tmp_path, path)
                finally:
                    if tmp_file is not None and not tmp_file.closed:
                        tmp_file.close()
                    if tmp_path:
                        try:
                            if os.path.exists(tmp_path):
                                os.unlink(tmp_path)
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Failed to save session {session.key}: {e}")
    
    def delete(self, key: str) -> bool:
        """
        Delete a session.
        
        Args:
            key: Session key.
        
        Returns:
            True if deleted, False if not found.
        """
        # Remove from cache
        self._cache.pop(key, None)
        
        # Remove file
        path = self._get_session_path(key)
        if path.exists():
            path.unlink()
            return True
        return False
    
    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.
        
        Returns:
            List of session info dicts.
        """
        sessions = []
        
        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path) as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            sessions.append({
                                "key": path.stem.replace("_", ":"),
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue
        
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
