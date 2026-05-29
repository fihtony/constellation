"""Tests for task status polling."""
import pytest
from framework.task_store import InMemoryTaskStore
from agents.compass.ui.routes import poll_task_status


class TestTaskPolling:
    def test_poll_returns_all_tasks(self):
        task_store = InMemoryTaskStore()
        task_store.create_task(agent_id="compass", metadata={"summary": "Task A"})

        result = poll_task_status(task_store=task_store, since=None)

        assert result["status"] == 200
        assert "application/json" in result["headers"]["Content-Type"]
        assert "tasks" in result["body"]
        assert "messages" in result["body"]
        assert "timestamp" in result["body"]