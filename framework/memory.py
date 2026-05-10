"""Memory service for cross-session long-term recall.

Inspired by ADK's MemoryService — stores facts, summaries, and knowledge
that persist beyond individual sessions.  Supports agent-scoped, project-scoped,
and global-scoped memories.
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
class MemoryEntry:
    """A single memory record."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    scope: str = "agent"  # "agent" | "project" | "global"
    scope_id: str = ""    # agent_id or project_id or ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class MemoryService(ABC):
    """Pluggable long-term memory backend."""

    @abstractmethod
    async def add(
        self,
        content: str,
        scope: str = "agent",
        scope_id: str = "",
        tags: list[str] | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Store a memory entry and return its ID."""
        ...

    @abstractmethod
    async def search(
        self,
        query: str,
        scope: str | None = None,
        scope_id: str | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Search memories by keyword match (full-text search in future)."""
        ...

    @abstractmethod
    async def delete(self, memory_id: str) -> None:
        ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------

class InMemoryMemoryService(MemoryService):
    """In-memory memory store for testing."""

    def __init__(self) -> None:
        self._entries: dict[str, MemoryEntry] = {}

    async def add(
        self,
        content: str,
        scope: str = "agent",
        scope_id: str = "",
        tags: list[str] | None = None,
        metadata: dict | None = None,
    ) -> str:
        entry = MemoryEntry(
            content=content,
            scope=scope,
            scope_id=scope_id,
            tags=tags or [],
            metadata=metadata or {},
        )
        self._entries[entry.id] = entry
        return entry.id

    async def search(
        self,
        query: str,
        scope: str | None = None,
        scope_id: str | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        results = list(self._entries.values())
        if scope:
            results = [e for e in results if e.scope == scope]
        if scope_id:
            results = [e for e in results if e.scope_id == scope_id]
        # Simple keyword matching for MVP
        query_lower = query.lower()
        results = [e for e in results if query_lower in e.content.lower()]
        return results[:limit]

    async def delete(self, memory_id: str) -> None:
        self._entries.pop(memory_id, None)


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------

class SqliteMemoryService(MemoryService):
    """SQLite-backed memory service for MVP."""

    def __init__(self, db_path: str = "data/memory.db") -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL DEFAULT 'agent',
                scope_id TEXT DEFAULT '',
                content TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, scope_id)")
        conn.commit()
        conn.close()

    async def add(
        self,
        content: str,
        scope: str = "agent",
        scope_id: str = "",
        tags: list[str] | None = None,
        metadata: dict | None = None,
    ) -> str:
        entry = MemoryEntry(
            content=content,
            scope=scope,
            scope_id=scope_id,
            tags=tags or [],
            metadata=metadata or {},
        )
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT INTO memories (id, scope, scope_id, content, tags, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id, entry.scope, entry.scope_id, entry.content,
                json.dumps(entry.tags),
                entry.created_at,
                json.dumps(entry.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()
        conn.close()
        return entry.id

    async def search(
        self,
        query: str,
        scope: str | None = None,
        scope_id: str | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        conn = sqlite3.connect(self._db_path)
        sql = "SELECT * FROM memories WHERE content LIKE ?"
        params: list[Any] = [f"%{query}%"]
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if scope_id:
            sql += " AND scope_id = ?"
            params.append(scope_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [
            MemoryEntry(
                id=r[0], scope=r[1], scope_id=r[2], content=r[3],
                tags=json.loads(r[4]),
                created_at=r[5],
                metadata=json.loads(r[6]),
            )
            for r in rows
        ]

    async def delete(self, memory_id: str) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        conn.close()
