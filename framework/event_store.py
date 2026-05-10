"""Event sourcing for audit and replay.

Every significant agent action (LLM call, tool call, node completion, state
change, decision, handoff) is recorded as an ``AgentEvent``.  All timestamps
use ISO 8601 with UTC offset.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class AgentEvent:
    """A single event in the agent's audit trail."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    session_id: str = ""
    agent_id: str = ""
    event_type: str = ""  # "llm_call", "tool_call", "node_completed", "state_change", "decision", "handoff"
    author: str = ""      # agent name or "user"
    content: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class EventStore(ABC):
    """Pluggable event persistence backend."""

    @abstractmethod
    async def append(
        self,
        session_id: str,
        event_type: str,
        content: dict,
        agent_id: str = "",
        author: str = "",
        metadata: dict | None = None,
    ) -> str:
        """Append an event and return its ID."""
        ...

    @abstractmethod
    async def list_events(
        self,
        session_id: str,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[AgentEvent]:
        ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------

class InMemoryEventStore(EventStore):
    """In-memory event store for testing."""

    def __init__(self) -> None:
        self._events: list[AgentEvent] = []

    async def append(
        self,
        session_id: str,
        event_type: str,
        content: dict,
        agent_id: str = "",
        author: str = "",
        metadata: dict | None = None,
    ) -> str:
        event = AgentEvent(
            session_id=session_id,
            event_type=event_type,
            content=content,
            agent_id=agent_id,
            author=author,
            metadata=metadata or {},
        )
        self._events.append(event)
        return event.id

    async def list_events(
        self,
        session_id: str,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[AgentEvent]:
        results = [e for e in self._events if e.session_id == session_id]
        if event_type:
            results = [e for e in results if e.event_type == event_type]
        if since:
            results = [e for e in results if e.timestamp > since]
        return results[:limit]


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------

class SqliteEventStore(EventStore):
    """SQLite-backed event store for MVP."""

    def __init__(self, db_path: str = "data/events.db") -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                session_id TEXT NOT NULL,
                agent_id TEXT DEFAULT '',
                event_type TEXT NOT NULL,
                author TEXT DEFAULT '',
                content TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp)")
        conn.commit()
        conn.close()

    async def append(
        self,
        session_id: str,
        event_type: str,
        content: dict,
        agent_id: str = "",
        author: str = "",
        metadata: dict | None = None,
    ) -> str:
        event = AgentEvent(
            session_id=session_id,
            event_type=event_type,
            content=content,
            agent_id=agent_id,
            author=author,
            metadata=metadata or {},
        )
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT INTO events (id, timestamp, session_id, agent_id, event_type, author, content, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id, event.timestamp, event.session_id, event.agent_id,
                event.event_type, event.author,
                json.dumps(event.content, ensure_ascii=False),
                json.dumps(event.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()
        conn.close()
        return event.id

    async def list_events(
        self,
        session_id: str,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[AgentEvent]:
        conn = sqlite3.connect(self._db_path)
        sql = "SELECT * FROM events WHERE session_id = ?"
        params: list[Any] = [session_id]
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        if since:
            sql += " AND timestamp > ?"
            params.append(since)
        sql += " ORDER BY timestamp ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [
            AgentEvent(
                id=r[0], timestamp=r[1], session_id=r[2], agent_id=r[3],
                event_type=r[4], author=r[5],
                content=json.loads(r[6]), metadata=json.loads(r[7]),
            )
            for r in rows
        ]
