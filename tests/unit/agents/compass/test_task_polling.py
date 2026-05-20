"""Tests for task status polling."""
import pytest
from agents.compass.ui.routes import poll_task_status


class TestTaskPolling:
    def test_poll_returns_all_tasks(self):
        # Mock task_store with some tasks
        result = poll_task_status(task_store=None, since=None)
        assert "tasks" in result
        assert "messages" in result  # New chat messages since last poll