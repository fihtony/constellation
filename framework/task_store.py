"""Persistent task store for A2A task lifecycle management.

Provides in-memory and SQLite-backed implementations for tracking task state,
artifacts, and metadata across agent invocations.  Every agent must use the
TaskStore for ``handle_message()`` and ``get_task()`` so that the polling
contract (``GET /tasks/{id}``) returns real state.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from framework.a2a.protocol import Artifact, Task, TaskState, TaskStatus, Message


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class TaskStore(ABC):
    """Abstract task persistence interface."""

    @abstractmethod
    def create_task(self, agent_id: str, metadata: dict | None = None) -> Task:
        """Create and persist a new task in SUBMITTED state."""
        ...

    @abstractmethod
    def get_task(self, task_id: str) -> Task | None:
        """Return the current task state, or None if not found."""
        ...

    @abstractmethod
    def update_state(
        self,
        task_id: str,
        state: TaskState,
        message: str = "",
    ) -> None:
        """Transition a task to a new state with optional status message."""
        ...

    @abstractmethod
    def add_artifact(self, task_id: str, artifact: Artifact) -> None:
        """Append an artifact to the task."""
        ...

    @abstractmethod
    def set_artifacts(self, task_id: str, artifacts: list[Artifact]) -> None:
        """Replace all artifacts for a task."""
        ...

    @abstractmethod
    def update_metadata(self, task_id: str, delta: dict) -> None:
        """Merge additional metadata into the task."""
        ...

    @abstractmethod
    def list_tasks(
        self,
        agent_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 100,
    ) -> list[Task]:
        """List tasks with optional filters."""
        ...

    # -- convenience helpers ------------------------------------------------

    def complete_task(
        self,
        task_id: str,
        artifacts: list[Artifact] | None = None,
        message: str = "",
    ) -> None:
        """Mark task completed and optionally set artifacts."""
        if artifacts is not None:
            self.set_artifacts(task_id, artifacts)
        self.update_state(task_id, TaskState.COMPLETED, message)

    def fail_task(self, task_id: str, error: str = "") -> None:
        """Mark task failed with error message."""
        self.update_state(task_id, TaskState.FAILED, error)

    def pause_task(
        self,
        task_id: str,
        question: str = "",
        interrupt_metadata: dict | None = None,
    ) -> None:
        """Mark task as requiring user input (interrupt)."""
        self.update_state(task_id, TaskState.INPUT_REQUIRED, question)
        if interrupt_metadata:
            self.update_metadata(task_id, {"_interrupt": interrupt_metadata})

    def resume_task(self, task_id: str) -> None:
        """Transition task from INPUT_REQUIRED back to WORKING."""
        self.update_state(task_id, TaskState.WORKING, "Resumed")

    def get_task_dict(self, task_id: str) -> dict:
        """Return task as A2A wire-format dict, or a FAILED stub if missing."""
        task = self.get_task(task_id)
        if task is None:
            return {
                "task": {
                    "id": task_id,
                    "status": {"state": TaskState.FAILED.value},
                    "artifacts": [],
                    "metadata": {},
                }
            }
        return task.to_dict()


# ---------------------------------------------------------------------------
# In-memory implementation (dev / testing)
# ---------------------------------------------------------------------------

class InMemoryTaskStore(TaskStore):
    """Thread-safe in-memory task store for development and testing."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._agent_index: dict[str, list[str]] = {}  # agent_id → [task_id]
        self._lock = threading.Lock()

    def create_task(self, agent_id: str, metadata: dict | None = None) -> Task:
        task = Task(metadata={"agentId": agent_id, **(metadata or {})})
        task.status = TaskStatus(state=TaskState.WORKING)
        with self._lock:
            self._tasks[task.id] = task
            self._agent_index.setdefault(agent_id, []).append(task.id)
        return task

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def update_state(
        self,
        task_id: str,
        state: TaskState,
        message: str = "",
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.transition(
                state,
                Message(parts=[{"text": message}]) if message else None,
            )

    def add_artifact(self, task_id: str, artifact: Artifact) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.artifacts.append(artifact)

    def set_artifacts(self, task_id: str, artifacts: list[Artifact]) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.artifacts = list(artifacts)

    def update_metadata(self, task_id: str, delta: dict) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.metadata.update(delta)

    def list_tasks(
        self,
        agent_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 100,
    ) -> list[Task]:
        with self._lock:
            if agent_id:
                ids = self._agent_index.get(agent_id, [])
                tasks = [self._tasks[tid] for tid in ids if tid in self._tasks]
            else:
                tasks = list(self._tasks.values())
            if state:
                tasks = [t for t in tasks if t.status.state == state]
            return tasks[:limit]


# ---------------------------------------------------------------------------
# SQLite-backed implementation (production MVP)
# ---------------------------------------------------------------------------

class SqliteTaskStore(TaskStore):
    """SQLite-backed task store for persistent deployments."""

    def __init__(self, db_path: str = "data/tasks.db") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'TASK_STATE_WORKING',
                status_message TEXT DEFAULT '',
                artifacts TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)"
        )
        conn.commit()
        conn.close()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def create_task(self, agent_id: str, metadata: dict | None = None) -> Task:
        task = Task(metadata={"agentId": agent_id, **(metadata or {})})
        task.status = TaskStatus(state=TaskState.WORKING)
        now = _now_iso()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO tasks (id, agent_id, state, artifacts, metadata, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        task.id,
                        agent_id,
                        task.status.state.value,
                        "[]",
                        json.dumps(task.metadata, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return task

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT id, state, status_message, artifacts, metadata FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        task_id_val, state_val, status_msg, artifacts_json, metadata_json = row
        task = Task(id=task_id_val, metadata=json.loads(metadata_json))
        task.status = TaskStatus(state=TaskState(state_val))
        if status_msg:
            task.status.message = Message(parts=[{"text": status_msg}])
        task.artifacts = [
            Artifact(
                name=a["name"],
                artifact_type=a.get("artifactType", "text/plain"),
                parts=a.get("parts", []),
                metadata=a.get("metadata", {}),
            )
            for a in json.loads(artifacts_json)
        ]
        return task

    def update_state(
        self,
        task_id: str,
        state: TaskState,
        message: str = "",
    ) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE tasks SET state = ?, status_message = ?, updated_at = ? WHERE id = ?",
                    (state.value, message, _now_iso(), task_id),
                )
                conn.commit()
            finally:
                conn.close()

    def add_artifact(self, task_id: str, artifact: Artifact) -> None:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT artifacts FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row:
                    arts = json.loads(row[0])
                    arts.append({
                        "name": artifact.name,
                        "artifactType": artifact.artifact_type,
                        "parts": artifact.parts,
                        "metadata": artifact.metadata,
                    })
                    conn.execute(
                        "UPDATE tasks SET artifacts = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(arts, ensure_ascii=False), _now_iso(), task_id),
                    )
                    conn.commit()
            finally:
                conn.close()

    def set_artifacts(self, task_id: str, artifacts: list[Artifact]) -> None:
        arts_json = json.dumps(
            [
                {
                    "name": a.name,
                    "artifactType": a.artifact_type,
                    "parts": a.parts,
                    "metadata": a.metadata,
                }
                for a in artifacts
            ],
            ensure_ascii=False,
        )
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE tasks SET artifacts = ?, updated_at = ? WHERE id = ?",
                    (arts_json, _now_iso(), task_id),
                )
                conn.commit()
            finally:
                conn.close()

    def update_metadata(self, task_id: str, delta: dict) -> None:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row:
                    meta = json.loads(row[0])
                    meta.update(delta)
                    conn.execute(
                        "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(meta, ensure_ascii=False), _now_iso(), task_id),
                    )
                    conn.commit()
            finally:
                conn.close()

    def list_tasks(
        self,
        agent_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 100,
    ) -> list[Task]:
        clauses = []
        params: list[Any] = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if state:
            clauses.append("state = ?")
            params.append(state.value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    f"SELECT id, state, status_message, artifacts, metadata FROM tasks{where} ORDER BY created_at DESC LIMIT ?",
                    params,
                ).fetchall()
            finally:
                conn.close()

        tasks = []
        for task_id_val, state_val, status_msg, arts_json, meta_json in rows:
            task = Task(id=task_id_val, metadata=json.loads(meta_json))
            task.status = TaskStatus(state=TaskState(state_val))
            if status_msg:
                task.status.message = Message(parts=[{"text": status_msg}])
            task.artifacts = [
                Artifact(
                    name=a["name"],
                    artifact_type=a.get("artifactType", "text/plain"),
                    parts=a.get("parts", []),
                    metadata=a.get("metadata", {}),
                )
                for a in json.loads(arts_json)
            ]
            tasks.append(task)
        return tasks
