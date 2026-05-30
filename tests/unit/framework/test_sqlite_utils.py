"""Tests for framework.sqlite_utils."""

from __future__ import annotations

import sqlite3

import pytest

from framework.sqlite_utils import connect_sqlite


def test_connect_sqlite_creates_parent_directory(tmp_path):
    db_path = tmp_path / "nested" / "state" / "tasks.db"

    conn = connect_sqlite(str(db_path))
    conn.execute("create table if not exists sample (id integer primary key)")
    conn.close()

    assert db_path.parent.is_dir()
    assert db_path.is_file()


def test_connect_sqlite_retries_unable_to_open_database_file(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    attempts = {"count": 0}
    real_connect = sqlite3.connect

    def flaky_connect(path):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(path)

    monkeypatch.setattr("framework.sqlite_utils.sqlite3.connect", flaky_connect)

    conn = connect_sqlite(str(db_path), retries=3, retry_delay_seconds=0)
    conn.close()

    assert attempts["count"] == 3


def test_connect_sqlite_does_not_swallow_other_operational_errors(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"

    def broken_connect(path):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr("framework.sqlite_utils.sqlite3.connect", broken_connect)

    with pytest.raises(sqlite3.OperationalError, match="malformed"):
        connect_sqlite(str(db_path), retries=3, retry_delay_seconds=0)