"""Tests for Compass UI routes."""
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
        result = handle_ui_request("GET", "/tasks", task_store=task_store)
        assert result["status"] == 200
        assert "application/json" in result["headers"]["Content-Type"]

    def test_get_task_detail(self, task_store):
        # Create a task first
        task = task_store.create_task(agent_id="compass", metadata={"summary": "Test task"})
        task_id = task.id

        result = handle_ui_request("GET", f"/tasks/{task_id}", task_store=task_store)
        assert result["status"] == 200
        data = result["body"]
        assert data["task_id"] == task_id