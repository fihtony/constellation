"""Tests for LogStore API endpoints."""
from pathlib import Path

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

    def test_get_logs_merges_filesystem_entries(self, tmp_path: Path):
        task_id = "task-123"
        agent_dir = tmp_path / task_id / "compass"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.log").write_text(
            "2026-05-28 10:30:00 [INFO] [compass] Accepted task\n",
            encoding="utf-8",
        )

        api = LogStoreAPI(LogStoreAgent(services=None, artifact_root=str(tmp_path)))
        result = api.get_logs(task_id)

        assert result["task_id"] == task_id
        assert len(result["logs"]) == 1
        assert result["logs"][0]["agent"] == "compass"
        assert result["logs"][0]["message"] == "Accepted task"

    def test_log_stream_route_exists(self):
        agent = LogStoreAgent(services=None)
        result = agent.serve_ui("/logs/stream/task-123")
        assert result["status"] == 200
        assert result["headers"]["Content-Type"].startswith("text/event-stream")

    def test_log_stream_emits_existing_entries(self):
        agent = LogStoreAgent(services=None)
        agent.add_log_sync(
            "task-123",
            {
                "timestamp": "2026-05-28 10:30:00",
                "level": "INFO",
                "agent": "compass",
                "message": "Accepted task",
            },
        )

        chunks = list(agent.stream_logs("task-123", max_iterations=0))

        assert any("event: log.appended" in chunk for chunk in chunks)
        assert any("Accepted task" in chunk for chunk in chunks)