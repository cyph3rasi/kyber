"""Central error-only log capture for the dashboard.

This module installs a Loguru sink at ERROR level and retains a bounded
in-memory list of error records. Optionally, it persists to a JSONL file
so errors can be viewed after restarts.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class ErrorRecord:
    ts: str
    level: str
    message: str
    where: str
    exception: str | None = None


class ErrorStore:
    def __init__(self, path: Path | None = None, max_items: int = 500):
        self._path = path
        self._max_items = max(50, int(max_items))
        self._lock = threading.Lock()
        self._items: list[ErrorRecord] = []
        self._load_tail()

    def _load_tail(self) -> None:
        if not self._path:
            return
        path = self._path.expanduser()
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-2000:]
        except Exception:
            return
        loaded: list[ErrorRecord] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                loaded.append(
                    ErrorRecord(
                        ts=str(obj.get("ts", "")),
                        level=str(obj.get("level", "ERROR")),
                        message=str(obj.get("message", "")),
                        where=str(obj.get("where", "")),
                        exception=obj.get("exception"),
                    )
                )
            except Exception:
                continue
        with self._lock:
            self._items = loaded[-self._max_items :]

    def _append_file(self, rec: ErrorRecord) -> None:
        if not self._path:
            return
        try:
            path = self._path.expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            obj = {
                "ts": rec.ts,
                "level": rec.level,
                "message": rec.message,
                "where": rec.where,
                "exception": rec.exception,
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=True) + "\n")
        except Exception:
            # Never let logging crash the app.
            return

    def ingest_loguru_record(self, record: dict[str, Any]) -> None:
        try:
            dt = record.get("time")
            if isinstance(dt, datetime):
                ts = dt.isoformat()
            else:
                # Loguru uses pendulum sometimes; string is fine.
                ts = str(dt)
            level = str(record.get("level", {}).get("name", "ERROR"))
            msg = str(record.get("message", ""))
            name = record.get("name") or "?"
            func = record.get("function") or "?"
            line = record.get("line") or "?"
            where = f"{name}:{func}:{line}"

            exc_obj = record.get("exception")
            exc: str | None = None
            if exc_obj:
                # Loguru exception object stringifies to a useful traceback.
                try:
                    exc = str(exc_obj)
                except Exception:
                    exc = repr(exc_obj)

            rec = ErrorRecord(ts=ts, level=level, message=msg, where=where, exception=exc)
        except Exception:
            return

        with self._lock:
            self._items.append(rec)
            if len(self._items) > self._max_items:
                self._items = self._items[-self._max_items :]

        self._append_file(rec)

    def get(self, limit: int = 200) -> list[dict[str, Any]]:
        n = max(1, min(int(limit), self._max_items))
        with self._lock:
            items = list(self._items[-n:])
        # Newest-first
        items.reverse()
        return [
            {
                "ts": r.ts,
                "level": r.level,
                "message": r.message,
                "where": r.where,
                "exception": r.exception,
            }
            for r in items
        ]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
        if self._path:
            try:
                self._path.expanduser().unlink(missing_ok=True)  # py3.11+
            except Exception:
                return


_STORE: ErrorStore | None = None
_SINK_ID: int | None = None


def init_error_store(path: Path | None = None, max_items: int = 500) -> ErrorStore:
    """Initialize global error store + loguru sink (idempotent)."""
    global _STORE, _SINK_ID
    if _STORE is None:
        _STORE = ErrorStore(path=path, max_items=max_items)

    if _SINK_ID is None:
        def _sink(message):  # type: ignore[no-untyped-def]
            # message.record is a dict[str, Any]
            try:
                if _STORE is not None:
                    _STORE.ingest_loguru_record(message.record)
            except Exception:
                return

        # Capture ERROR and above. Avoid diagnose to keep traces smaller.
        _SINK_ID = logger.add(_sink, level="ERROR", backtrace=True, diagnose=False)
    return _STORE


def get_errors(limit: int = 200) -> list[dict[str, Any]]:
    if _STORE is None:
        return []
    return _STORE.get(limit=limit)


def clear_errors() -> None:
    if _STORE is None:
        return
    _STORE.clear()

