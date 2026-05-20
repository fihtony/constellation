"""Tests for LogStore API endpoints."""
import pytest
from agents.log_store.agent import LogStoreAgent
from agents.log_store.api import LogStoreAPI


class TestLogStoreAPI:
    def test_get_logs_empty(self):
        api = LogStoreAPI(LogStoreAgent(services=None))
        result = api.get_logs("task-123")
        assert result["task_id"] == "task-123"
        assert result["logs"] == []

    def test_add_log(self):
        api = LogStoreAPI(LogStoreAgent(services=None))
        api.add_log("task-123", {
            "timestamp": "2026-05-20 10:30:00",
            "level": "ERROR",
            "agent": "scm",
            "message": "Access denied",
        })
        logs = api.get_logs("task-123")["logs"]
        assert len(logs) == 1
        assert logs[0]["level"] == "ERROR"

    def test_health(self):
        api = LogStoreAPI(LogStoreAgent(services=None))
        result = api.health()
        assert result["status"] == "ok"