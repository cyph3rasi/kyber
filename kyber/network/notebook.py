"""The shared notebook — a small append-oriented key/value store.

Lives at ``~/.kyber/notebook.db`` on the **host** machine only. Every
paired Kyber (host or spoke) can read and write it through the network
link: hosts call this module directly, spokes issue RPCs over the
WebSocket that route to these same functions on the host.

Schema (one SQLite table):

.. code-block:: text

    entries(
      id              INTEGER PK,
      key             TEXT NOT NULL,
      value           TEXT NOT NULL,            -- free-form; agents often store JSON
      tags            TEXT NOT NULL DEFAULT '', -- comma-separated, lowercased
      author_peer_id  TEXT NOT NULL,            -- who wrote it (permanent UUID)
      author_name     TEXT NOT NULL DEFAULT '', -- display name at write time
      created_at      REAL NOT NULL,
      updated_at      REAL NOT NULL
    )

The ``key`` is intentionally not unique — writing to the same key appends
a new entry, so agents can leave "yesterday's note" and "today's note"
under the same topic and everything stays legible as history. Callers
who want upsert semantics can use :func:`write` with ``replace=True``,
which nukes prior entries sharing the exact key before writing.

Search is substring-based over key, value, and tags. Good enough for a
notebook; semantic search is explicitly out of scope for this phase.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

NOTEBOOK_DB_PATH = Path.home() / ".kyber" / "notebook.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '',
    author_peer_id  TEXT NOT NULL,
    author_name     TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_key ON entries(key);
CREATE INDEX IF NOT EXISTS idx_entries_created ON entries(created_at DESC);
"""

_lock = threading.Lock()


@dataclass
class NotebookEntry:
    id: int
    key: str
    value: str
    tags: list[str]
    author_peer_id: str
    author_name: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or NOTEBOOK_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _row_to_entry(row: sqlite3.Row) -> NotebookEntry:
    raw_tags = row["tags"] or ""
    return NotebookEntry(
        id=int(row["id"]),
        key=str(row["key"]),
        value=str(row["value"]),
        tags=[t for t in raw_tags.split(",") if t],
        author_peer_id=str(row["author_peer_id"]),
        author_name=str(row["author_name"] or ""),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _normalize_tags(tags: Iterable[str] | None) -> str:
    if not tags:
        return ""
    cleaned: list[str] = []
    for t in tags:
        if not isinstance(t, str):
            continue
        s = t.strip().lower()
        if s and "," not in s:
            cleaned.append(s)
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for s in cleaned:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return ",".join(out)


def write(
    key: str,
    value: str,
    *,
    author_peer_id: str,
    author_name: str = "",
    tags: Iterable[str] | None = None,
    replace: bool = False,
    path: Path | None = None,
) -> NotebookEntry:
    """Append a new entry (or overwrite prior entries with the same key).

    ``value`` is opaque — usually plain text or JSON. If you pass a dict
    or list, we'll serialize it for you.
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key is required")
    if not isinstance(author_peer_id, str) or not author_peer_id.strip():
        raise ValueError("author_peer_id is required")

    if not isinstance(value, str):
        # Friendliness: accept structured values from callers.
        try:
            value = json.dumps(value, sort_keys=True)
        except TypeError as e:
            raise ValueError(f"value must be str or JSON-serializable: {e}") from e

    tag_str = _normalize_tags(tags)
    now = time.time()
    with _lock, _connect(path) as conn:
        if replace:
            conn.execute("DELETE FROM entries WHERE key = ?", (key,))
        cur = conn.execute(
            """
            INSERT INTO entries (key, value, tags, author_peer_id, author_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (key, value, tag_str, author_peer_id, author_name or "", now, now),
        )
        entry_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
    return _row_to_entry(row)


def read(
    key: str,
    *,
    limit: int = 1,
    path: Path | None = None,
) -> list[NotebookEntry]:
    """Read entries for a key, newest first. ``limit=0`` returns all history."""
    if not isinstance(key, str) or not key.strip():
        return []
    with _lock, _connect(path) as conn:
        if limit and limit > 0:
            rows = conn.execute(
                "SELECT * FROM entries WHERE key = ? ORDER BY created_at DESC LIMIT ?",
                (key, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM entries WHERE key = ? ORDER BY created_at DESC", (key,)
            ).fetchall()
    return [_row_to_entry(r) for r in rows]


def list_entries(
    *,
    tag: str | None = None,
    limit: int = 50,
    path: Path | None = None,
) -> list[NotebookEntry]:
    """List the most recent entries, optionally filtered by a tag."""
    limit = max(1, min(500, int(limit)))
    with _lock, _connect(path) as conn:
        if tag:
            t = tag.strip().lower()
            rows = conn.execute(
                """
                SELECT * FROM entries
                WHERE (',' || tags || ',') LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"%,{t},%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM entries ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_entry(r) for r in rows]


def search(
    query: str,
    *,
    limit: int = 50,
    path: Path | None = None,
) -> list[NotebookEntry]:
    """Substring search over key + value + tags. Case-insensitive."""
    q = (query or "").strip()
    if not q:
        return list_entries(limit=limit, path=path)
    like = f"%{q.lower()}%"
    limit = max(1, min(500, int(limit)))
    with _lock, _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM entries
            WHERE lower(key) LIKE ? OR lower(value) LIKE ? OR lower(tags) LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
    return [_row_to_entry(r) for r in rows]


def delete_entry(entry_id: int, *, path: Path | None = None) -> bool:
    with _lock, _connect(path) as conn:
        cur = conn.execute("DELETE FROM entries WHERE id = ?", (int(entry_id),))
        return cur.rowcount > 0


def stats(*, path: Path | None = None) -> dict[str, Any]:
    with _lock, _connect(path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        authors = conn.execute(
            "SELECT author_name, COUNT(*) AS n FROM entries GROUP BY author_name ORDER BY n DESC"
        ).fetchall()
    return {
        "total": int(total),
        "by_author": [{"name": r[0] or "(unknown)", "count": int(r[1])} for r in authors],
    }
