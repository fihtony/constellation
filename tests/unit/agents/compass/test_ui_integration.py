"""Tests for Compass UI routes."""
from pathlib import Path

import pytest
from framework.task_store import InMemoryTaskStore
from agents.compass.ui.routes import handle_ui_request


class TestUIRoutes:
    @pytest.fixture()
    def task_store(self):
        return InMemoryTaskStore()

    def test_get_ui_returns_html(self):
        result = handle_ui_request("GET", "/ui")
        assert result["status"] == 200
        assert "text/html" in result["headers"]["Content-Type"]
        assert "Compass Agent" in result["body"]

    def test_get_tasks_returns_json(self, task_store):
        task_store.create_task(
            agent_id="compass",
            metadata={
                "summary": "Test task",
                "userRequest": "Please summarize this",
                "chat_history": [{"role": "USER", "text": "Please summarize this", "tone": "normal", "ts": "2026-05-28T00:00:00+00:00"}],
            },
        )
        result = handle_ui_request("GET", "/tasks", task_store=task_store)
        assert result["status"] == 200
        assert "application/json" in result["headers"]["Content-Type"]
        assert result["body"]["tasks"]
        first = result["body"]["tasks"][0]
        assert "createdAt" in first
        assert "updatedAt" in first
        assert first["userRequest"] == "Please summarize this"
        assert first["chatHistory"][0]["text"] == "Please summarize this"

    def test_get_task_detail(self, task_store):
        # Create a task first
        task = task_store.create_task(agent_id="compass", metadata={"summary": "Test task"})
        task_id = task.id

        result = handle_ui_request("GET", f"/tasks/{task_id}", task_store=task_store)
        assert result["status"] == 200
        data = result["body"]
        assert data["task_id"] == task_id

    def test_ui_events_route_exists(self, task_store):
        result = handle_ui_request("GET", "/ui/events", task_store=task_store)
        assert result["status"] == 200
        assert result["headers"]["Content-Type"].startswith("text/event-stream")

    def test_logs_route_uses_filesystem_fallback(self, monkeypatch, tmp_path: Path):
        task_id = "task-logs"
        agent_dir = tmp_path / task_id / "compass"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.log").write_text(
            "2026-05-28 10:30:00 [INFO] [compass] Accepted task\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

        result = handle_ui_request("GET", f"/logs/{task_id}")

        assert result["status"] == 200
        assert result["body"]["logs"][0]["task_id"] == task_id
        assert result["body"]["logs"][0]["message"] == "Accepted task"