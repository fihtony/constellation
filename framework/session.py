"""Session management with pluggable backends.

A Session holds per-conversation state for an agent.  All datetime fields use
ISO 8601 with timezone offset (UTC).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    """ISO 8601 timestamp with UTC offset."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Session:
    """A single agent conversation / task context."""

    id: str
    agent_id: str
    user_id: str
    state: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class SessionService(ABC):
    """Pluggable session persistence backend."""

    @abstractmethod
    async def create(self, agent_id: str, user_id: str, metadata: dict | None = None) -> Session:
        ...

    @abstractmethod
    async def get(self, session_id: str) -> Session | None:
        ...

    @abstractmethod
    async def update_state(self, session_id: str, delta: dict) -> None:
        ...

    @abstractmethod
    async def list_sessions(
        self,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> list[Session]:
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        ...


# ---------------------------------------------------------------------------
# In-memory implementation (tests / dev)
# ---------------------------------------------------------------------------

class InMemorySessionService(SessionService):
    """In-memory session store — no persistence across restarts."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def create(self, agent_id: str, user_id: str, metadata: dict | None = None) -> Session:
        s = Session(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            user_id=user_id,
            metadata=metadata or {},
        )
        self._sessions[s.id] = s
        return s

    async def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def update_state(self, session_id: str, delta: dict) -> None:
        s = self._sessions.get(session_id)
        if s:
            s.state.update(delta)
            s.updated_at = _now_iso()

    async def list_sessions(
        self,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> list[Session]:
        results = list(self._sessions.values())
        if agent_id:
            results = [s for s in results if s.agent_id == agent_id]
        if user_id:
            results = [s for s in results if s.user_id == user_id]
        return results

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# ---------------------------------------------------------------------------
# SQLite implementation (MVP production)
# ---------------------------------------------------------------------------

class SqliteSessionService(SessionService):
    """SQLite-backed session persistence for MVP."""

    def __init__(self, db_path: str = "data/sessions.db") -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                state TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
        conn.commit()
        conn.close()

    async def create(self, agent_id: str, user_id: str, metadata: dict | None = None) -> Session:
        s = Session(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            user_id=user_id,
            metadata=metadata or {},
        )
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT INTO sessions (id, agent_id, user_id, state, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                s.id, s.agent_id, s.user_id,
                json.dumps(s.state, ensure_ascii=False),
                json.dumps(s.metadata, ensure_ascii=False),
                s.created_at, s.updated_at,
            ),
        )
        conn.commit()
        conn.close()
        return s

    async def get(self, session_id: str) -> Session | None:
        conn = sqlite3.connect(self._db_path)
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        conn.close()
        if not row:
            return None
        return Session(
            id=row[0], agent_id=row[1], user_id=row[2],
            state=json.loads(row[3]),
            metadata=json.loads(row[4]),
            created_at=row[5], updated_at=row[6],
        )

    async def update_state(self, session_id: str, delta: dict) -> None:
        conn = sqlite3.connect(self._db_path)
        row = conn.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row:
            state = json.loads(row[0])
            state.update(delta)
            conn.execute(
                "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
                (json.dumps(state, ensure_ascii=False), _now_iso(), session_id),
            )
            conn.commit()
        conn.close()

    async def list_sessions(
        self,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> list[Session]:
        conn = sqlite3.connect(self._db_path)
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if agent_id:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY created_at ASC"
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [
            Session(
                id=r[0], agent_id=r[1], user_id=r[2],
                state=json.loads(r[3]),
                metadata=json.loads(r[4]),
                created_at=r[5], updated_at=r[6],
            )
            for r in rows
        ]

    async def delete(self, session_id: str) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        conn.close()
