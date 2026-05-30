"""Shared helpers for resilient SQLite access in persistent agents."""

from __future__ import annotations

import os
import sqlite3
import time


def connect_sqlite(
    db_path: str,
    *,
    retries: int = 3,
    retry_delay_seconds: float = 0.05,
) -> sqlite3.Connection:
    """Open a SQLite database, retrying transient open failures.

    Docker bind mounts can briefly report "unable to open database file" during
    container/task churn. Ensure the parent directory exists and retry a few
    times before surfacing the error.
    """

    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    attempt = 0
    while True:
        try:
            return sqlite3.connect(db_path)
        except sqlite3.OperationalError as exc:
            attempt += 1
            if "unable to open database file" not in str(exc).lower() or attempt > retries:
                raise
            time.sleep(retry_delay_seconds)