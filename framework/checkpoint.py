"""Workflow checkpoint service for interrupt/resume and crash recovery.

All data is JSON-serialized.  Timestamps use ISO 8601 + UTC.
"""
from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class CheckpointService(ABC):
    """Pluggable checkpoint persistence."""

    @abstractmethod
    async def save(self, session_id: str, thread_id: str, data: dict) -> None:
        ...

    @abstractmethod
    async def load(self, session_id: str, thread_id: str) -> dict | None:
        ...

    @abstractmethod
    async def delete(self, session_id: str, thread_id: str) -> None:
        ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------

class InMemoryCheckpointer(CheckpointService):
    """In-memory checkpoint store for testing."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], dict] = {}

    async def save(self, session_id: str, thread_id: str, data: dict) -> None:
        self._store[(session_id, thread_id)] = data

    async def load(self, session_id: str, thread_id: str) -> dict | None:
        return self._store.get((session_id, thread_id))

    async def delete(self, session_id: str, thread_id: str) -> None:
        self._store.pop((session_id, thread_id), None)


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------

class SqliteCheckpointer(CheckpointService):
    """SQLite-backed checkpoint persistence for MVP."""

    def __init__(self, db_path: str = "data/checkpoints.db") -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                session_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, thread_id)
            )
        """)
        conn.commit()
        conn.close()

    async def save(self, session_id: str, thread_id: str, data: dict) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints (session_id, thread_id, data, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                session_id, thread_id,
                json.dumps(data, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

    async def load(self, session_id: str, thread_id: str) -> dict | None:
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT data FROM checkpoints WHERE session_id = ? AND thread_id = ?",
            (session_id, thread_id),
        ).fetchone()
        conn.close()
        return json.loads(row[0]) if row else None

    async def delete(self, session_id: str, thread_id: str) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "DELETE FROM checkpoints WHERE session_id = ? AND thread_id = ?",
            (session_id, thread_id),
        )
        conn.commit()
        conn.close()
