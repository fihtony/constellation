"""SQLite persistence layer for Teams Gateway."""

from __future__ import annotations

import os
import sqlite3
import threading
import time

_SCHEMA_VERSION = 1

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS conversation_references (
    user_aad_id           TEXT NOT NULL,
    tenant_id             TEXT NOT NULL,
    conversation_id       TEXT NOT NULL,
    service_url           TEXT NOT NULL,
    bot_id                TEXT NOT NULL,
    is_valid              INTEGER NOT NULL DEFAULT 1,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (user_aad_id, tenant_id)
);

CREATE TABLE IF NOT EXISTS user_task_mapping (
    task_id          TEXT NOT NULL PRIMARY KEY,
    user_aad_id      TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    last_known_state TEXT NOT NULL DEFAULT 'SUBMITTED',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_utm_user
    ON user_task_mapping(user_aad_id, tenant_id);

CREATE INDEX IF NOT EXISTS idx_utm_state
    ON user_task_mapping(last_known_state);

CREATE TABLE IF NOT EXISTS activity_dedup (
    activity_id   TEXT NOT NULL PRIMARY KEY,
    processed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_aad_id   TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload       TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nq_retry
    ON notification_queue(status, next_retry_at)
    WHERE status = 'pending';
"""


class GatewayDB:
    """Thread-safe SQLite wrapper for Teams Gateway persistence."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or os.environ.get(
            "TEAMS_GATEWAY_DB_PATH",
            "/app/data/teams-gateway/teams-gateway.db",
        )
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            conn.executescript(_CREATE_TABLES)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < _SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        print(f"[teams-gateway] Database initialized: {self._db_path} (schema v{_SCHEMA_VERSION})")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---- Conversation References ----

    def upsert_conversation_ref(
        self,
        user_aad_id: str,
        tenant_id: str,
        conversation_id: str,
        service_url: str,
        bot_id: str,
    ):
        now = _iso_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO conversation_references
                       (user_aad_id, tenant_id, conversation_id, service_url, bot_id,
                        is_valid, consecutive_failures, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?)
                       ON CONFLICT(user_aad_id, tenant_id) DO UPDATE SET
                         conversation_id = excluded.conversation_id,
                         service_url = excluded.service_url,
                         bot_id = excluded.bot_id,
                         is_valid = 1,
                         consecutive_failures = 0,
                         updated_at = excluded.updated_at""",
                    (user_aad_id, tenant_id, conversation_id, service_url, bot_id, now, now),
                )
                conn.commit()
            finally:
                conn.close()

    def get_conversation_ref(self, user_aad_id: str, tenant_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM conversation_references WHERE user_aad_id=? AND tenant_id=?",
                    (user_aad_id, tenant_id),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def delete_conversation_ref(self, user_aad_id: str, tenant_id: str):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM conversation_references WHERE user_aad_id=? AND tenant_id=?",
                    (user_aad_id, tenant_id),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_conversation_invalid(self, user_aad_id: str, tenant_id: str):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """UPDATE conversation_references SET is_valid=0, updated_at=?
                       WHERE user_aad_id=? AND tenant_id=?""",
                    (_iso_now(), user_aad_id, tenant_id),
                )
                conn.commit()
            finally:
                conn.close()

    def increment_failure(self, user_aad_id: str, tenant_id: str) -> int:
        """Increment consecutive_failures, auto-invalidate at 5. Returns new count."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """UPDATE conversation_references
                       SET consecutive_failures = consecutive_failures + 1, updated_at=?
                       WHERE user_aad_id=? AND tenant_id=?""",
                    (_iso_now(), user_aad_id, tenant_id),
                )
                row = conn.execute(
                    "SELECT consecutive_failures FROM conversation_references WHERE user_aad_id=? AND tenant_id=?",
                    (user_aad_id, tenant_id),
                ).fetchone()
                count = row[0] if row else 0
                if count >= 5:
                    conn.execute(
                        "UPDATE conversation_references SET is_valid=0 WHERE user_aad_id=? AND tenant_id=?",
                        (user_aad_id, tenant_id),
                    )
                conn.commit()
                return count
            finally:
                conn.close()

    # ---- User-Task Mapping ----

    def add_task_mapping(self, task_id: str, user_aad_id: str, tenant_id: str):
        now = _iso_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO user_task_mapping
                       (task_id, user_aad_id, tenant_id, last_known_state, created_at, updated_at)
                       VALUES (?, ?, ?, 'SUBMITTED', ?, ?)""",
                    (task_id, user_aad_id, tenant_id, now, now),
                )
                conn.commit()
            finally:
                conn.close()

    def update_task_state(self, task_id: str, state: str):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE user_task_mapping SET last_known_state=?, updated_at=? WHERE task_id=?",
                    (state, _iso_now(), task_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_user_tasks(self, user_aad_id: str, tenant_id: str) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT * FROM user_task_mapping
                       WHERE user_aad_id=? AND tenant_id=?
                       ORDER BY created_at DESC""",
                    (user_aad_id, tenant_id),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_task_owner(self, task_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT user_aad_id, tenant_id FROM user_task_mapping WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def count_active_tasks(self, user_aad_id: str, tenant_id: str) -> int:
        terminal = ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED")
        placeholders = ",".join("?" for _ in terminal)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"""SELECT COUNT(*) FROM user_task_mapping
                       WHERE user_aad_id=? AND tenant_id=? AND last_known_state NOT IN ({placeholders})""",
                    (user_aad_id, tenant_id) + terminal,
                ).fetchone()
                return row[0] if row else 0
            finally:
                conn.close()

    # ---- Activity Dedup ----

    def check_and_record_activity(self, activity_id: str) -> bool:
        """Return True if activity was already processed (duplicate)."""
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

    def cleanup_old_activities(self, max_age_seconds: int = 600):
        cutoff = _iso_from_ts(time.time() - max_age_seconds)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM activity_dedup WHERE processed_at < ?",
                    (cutoff,),
                )
                conn.commit()
            finally:
                conn.close()

    def cleanup_old_task_mappings(self, max_age_days: int = 30):
        cutoff = _iso_from_ts(time.time() - max_age_days * 86400)
        terminal = ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED")
        placeholders = ",".join("?" for _ in terminal)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"""DELETE FROM user_task_mapping
                       WHERE last_known_state IN ({placeholders}) AND updated_at < ?""",
                    terminal + (cutoff,),
                )
                conn.commit()
            finally:
                conn.close()


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso_from_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
