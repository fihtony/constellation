"""Unified persistence layer for IM Gateway.

Platform-agnostic schema as specified in docs/compass-slack-integration-zh.md §3.3.
Replaces the Teams-specific DB schema with generic channel/user_id/workspace fields.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

_SCHEMA_VERSION = 2

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS conversations (
    channel       TEXT NOT NULL,   -- "teams" | "slack" | "lark" | ...
    user_id       TEXT NOT NULL,   -- platform-specific user identifier
    workspace     TEXT NOT NULL,   -- tenant_id / team_id
    target        TEXT NOT NULL,   -- JSON: platform-specific delivery target
    is_valid      INTEGER DEFAULT 1,
    failures      INTEGER DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (channel, user_id, workspace)
);

CREATE TABLE IF NOT EXISTS user_task_mapping (
    task_id    TEXT NOT NULL PRIMARY KEY,
    channel    TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    workspace  TEXT NOT NULL,
    thread_ref TEXT,              -- Teams: conversation_id; Slack: thread_ts
    state      TEXT NOT NULL DEFAULT 'SUBMITTED',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_utm_user
    ON user_task_mapping(channel, user_id, workspace);

CREATE INDEX IF NOT EXISTS idx_utm_state
    ON user_task_mapping(state);

CREATE TABLE IF NOT EXISTS activity_dedup (
    activity_id   TEXT NOT NULL PRIMARY KEY,
    processed_at  TEXT NOT NULL
);
"""


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso_from_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class GatewayDB:
    """Thread-safe SQLite wrapper for IM Gateway persistence."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or os.environ.get(
            "IM_GATEWAY_DB_PATH",
            "/app/data/im-gateway/im-gateway.db",
        )
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode = DELETE")  # safe across mounts
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(_CREATE_TABLES)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < _SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        print(f"[im-gateway] Database initialized: {self._db_path} (schema v{_SCHEMA_VERSION})")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---- Conversations (replaces conversation_references) ----

    def upsert_conversation(
        self,
        channel: str,
        user_id: str,
        workspace: str,
        target: dict,
    ) -> None:
        import json as _json
        now = _iso_now()
        target_json = _json.dumps(target, ensure_ascii=False)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO conversations
                       (channel, user_id, workspace, target, is_valid, failures, updated_at)
                       VALUES (?, ?, ?, ?, 1, 0, ?)
                       ON CONFLICT(channel, user_id, workspace) DO UPDATE SET
                         target = excluded.target,
                         is_valid = 1,
                         failures = 0,
                         updated_at = excluded.updated_at""",
                    (channel, user_id, workspace, target_json, now),
                )
                conn.commit()
            finally:
                conn.close()

    def get_conversation(self, channel: str, user_id: str, workspace: str) -> dict | None:
        import json as _json
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM conversations WHERE channel=? AND user_id=? AND workspace=?",
                    (channel, user_id, workspace),
                ).fetchone()
                if not row:
                    return None
                result = dict(row)
                result["target"] = _json.loads(result["target"])
                return result
            finally:
                conn.close()

    def delete_conversation(self, channel: str, user_id: str, workspace: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM conversations WHERE channel=? AND user_id=? AND workspace=?",
                    (channel, user_id, workspace),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_conversation_invalid(self, channel: str, user_id: str, workspace: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """UPDATE conversations SET is_valid=0, updated_at=?
                       WHERE channel=? AND user_id=? AND workspace=?""",
                    (_iso_now(), channel, user_id, workspace),
                )
                conn.commit()
            finally:
                conn.close()

    def increment_failure(self, channel: str, user_id: str, workspace: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """UPDATE conversations
                       SET failures = failures + 1, updated_at=?
                       WHERE channel=? AND user_id=? AND workspace=?""",
                    (_iso_now(), channel, user_id, workspace),
                )
                row = conn.execute(
                    "SELECT failures FROM conversations WHERE channel=? AND user_id=? AND workspace=?",
                    (channel, user_id, workspace),
                ).fetchone()
                count = row[0] if row else 0
                if count >= 5:
                    conn.execute(
                        "UPDATE conversations SET is_valid=0 WHERE channel=? AND user_id=? AND workspace=?",
                        (channel, user_id, workspace),
                    )
                conn.commit()
                return count
            finally:
                conn.close()

    # ---- User-Task Mapping ----

    def add_task_mapping(
        self,
        task_id: str,
        channel: str,
        user_id: str,
        workspace: str,
        thread_ref: str = "",
    ) -> None:
        now = _iso_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO user_task_mapping
                       (task_id, channel, user_id, workspace, thread_ref, state, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'SUBMITTED', ?, ?)""",
                    (task_id, channel, user_id, workspace, thread_ref, now, now),
                )
                conn.commit()
            finally:
                conn.close()

    def update_task_state(self, task_id: str, state: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE user_task_mapping SET state=?, updated_at=? WHERE task_id=?",
                    (state, _iso_now(), task_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_user_tasks(self, channel: str, user_id: str, workspace: str) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT * FROM user_task_mapping
                       WHERE channel=? AND user_id=? AND workspace=?
                       ORDER BY created_at DESC""",
                    (channel, user_id, workspace),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_task_owner(self, task_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT channel, user_id, workspace, thread_ref FROM user_task_mapping WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def count_active_tasks(self, channel: str, user_id: str, workspace: str) -> int:
        terminal = ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED")
        placeholders = ",".join("?" for _ in terminal)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"""SELECT COUNT(*) FROM user_task_mapping
                       WHERE channel=? AND user_id=? AND workspace=? AND state NOT IN ({placeholders})""",
                    (channel, user_id, workspace) + terminal,
                ).fetchone()
                return row[0] if row else 0
            finally:
                conn.close()

    # ---- Activity Dedup ----

    def check_and_record_activity(self, activity_id: str) -> bool:
        now = _iso_now()
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT 1 FROM activity_dedup WHERE activity_id=?",
                    (activity_id,),
                ).fetchone()
                if existing:
                    return True
                conn.execute(
                    "INSERT INTO activity_dedup (activity_id, processed_at) VALUES (?, ?)",
                    (activity_id, now),
                )
                conn.commit()
                return False
            finally:
                conn.close()

    def cleanup_old_activities(self, max_age_seconds: int = 600) -> None:
        cutoff = _iso_from_ts(time.time() - max_age_seconds)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM activity_dedup WHERE processed_at < ?", (cutoff,))
                conn.commit()
            finally:
                conn.close()

    def cleanup_old_task_mappings(self, max_age_days: int = 30) -> None:
        cutoff = _iso_from_ts(time.time() - max_age_days * 86400)
        terminal = ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED")
        placeholders = ",".join("?" for _ in terminal)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"""DELETE FROM user_task_mapping
                       WHERE state IN ({placeholders}) AND updated_at < ?""",
                    terminal + (cutoff,),
                )
                conn.commit()
            finally:
                conn.close()
