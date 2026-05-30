"""Unit tests for framework.lifecycle — PerTaskLifecycleManager."""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from framework.lifecycle import (
    EXIT_ACK,
    EXIT_IDLE_TIMEOUT,
    EXIT_TERMINATE,
    LifecycleState,
    PerTaskLifecycleManager,
)


class TestLifecycleStates:
    """Test state transitions."""

    def test_initial_state(self):
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        assert lm.state == LifecycleState.SUBMITTED_WAITING

    def test_mark_working(self):
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.mark_working("task-1")
        assert lm.state == LifecycleState.WORKING
        assert lm.current_task_id == "task-1"

    def test_mark_working_updates_registry_status(self):
        updater = MagicMock()
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.configure_registry_updater(updater)

        lm.mark_working("task-1")

        updater.assert_called_once_with(status="busy", current_task_id="task-1")

    def test_arm_idle_timer(self):
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.mark_working("task-1")
        lm.arm_idle_timer("task-1")
        assert lm.state == LifecycleState.IDLE_WAITING
        # Cleanup
        lm.cancel_idle_timer()

    def test_arm_idle_timer_updates_registry_status(self):
        updater = MagicMock()
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.configure_registry_updater(updater)

        lm.mark_working("task-1")
        updater.reset_mock()
        lm.arm_idle_timer("task-1")

        updater.assert_called_once_with(status="idle", current_task_id="task-1")
        lm.cancel_idle_timer()

    def test_cancel_idle_timer(self):
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.arm_idle_timer("task-1")
        lm.cancel_idle_timer()
        # Timer should be cancelled, state still IDLE_WAITING (cancel doesn't change state)
        assert lm._timer is None


class TestAckHandling:
    """Test ACK-triggered shutdown."""

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_handle_ack_triggers_exit(self, mock_exit):
        updater = MagicMock()
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.configure_registry_updater(updater)
        lm.arm_idle_timer("task-1")

        result = lm.handle_ack("task-1")

        assert result["status"] == "ok"
        assert "shutting down" in result["message"]
        assert lm.state == LifecycleState.SHUTTING_DOWN
        updater.assert_called_with(status="exited", current_task_id=None)
        mock_exit.assert_called_once_with(EXIT_ACK, reason="ACK received for task task-1")

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_handle_ack_idempotent(self, mock_exit):
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.arm_idle_timer("task-1")

        lm.handle_ack("task-1")
        result2 = lm.handle_ack("task-1")

        assert "already acknowledged" in result2["message"]
        # Only one exit scheduled
        mock_exit.assert_called_once()

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_ack_cancels_idle_timer(self, mock_exit):
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.arm_idle_timer("task-1")
        assert lm._timer is not None

        lm.handle_ack("task-1")
        assert lm._timer is None


class TestPingHandling:
    """Test keep-alive ping handling."""

    def test_ping_resets_timer_in_idle_state(self):
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.arm_idle_timer("task-1")
        old_timer = lm._timer

        result = lm.handle_ping("task-1")

        assert result["status"] == "ok"
        assert "timer reset" in result["message"]
        # A new timer should have been created
        assert lm._timer is not None
        assert lm._timer is not old_timer
        lm.cancel_idle_timer()

    def test_ping_noop_in_working_state(self):
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.mark_working("task-1")

        result = lm.handle_ping("task-1")

        assert "no timer reset" in result["message"]


class TestTerminateHandling:
    """Test forced termination."""

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_handle_terminate(self, mock_exit):
        updater = MagicMock()
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=10)
        lm.configure_registry_updater(updater)
        lm.mark_working("task-1")

        result = lm.handle_terminate("task-1")

        assert result["status"] == "ok"
        assert "terminating" in result["message"]
        assert lm.state == LifecycleState.SHUTTING_DOWN
        updater.assert_called_with(status="exited", current_task_id=None)
        mock_exit.assert_called_once_with(EXIT_TERMINATE, reason="Terminate requested for task task-1")


class TestIdleTimeout:
    """Test idle timeout fires correctly."""

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_idle_timeout_fires(self, mock_exit):
        notify_mock = MagicMock()
        updater = MagicMock()
        lm = PerTaskLifecycleManager(
            agent_id="test",
            idle_timeout_seconds=0.1,  # Very short for testing
            on_timeout_notify=notify_mock,
        )
        lm.configure_registry_updater(updater)
        lm.arm_idle_timer("task-1")

        # Wait for timeout to fire
        time.sleep(0.3)

        assert lm.state == LifecycleState.SHUTTING_DOWN
        mock_exit.assert_called_once_with(EXIT_IDLE_TIMEOUT, reason="idle timeout")
        notify_mock.assert_called_once_with("task-1")
        updater.assert_called_with(status="exited", current_task_id=None)

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_idle_timeout_cancelled_by_new_work(self, mock_exit):
        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=0.2)
        lm.arm_idle_timer("task-1")

        # Cancel before timeout fires
        time.sleep(0.05)
        lm.cancel_idle_timer()
        lm.mark_working("task-2")

        # Wait past what would have been timeout
        time.sleep(0.3)

        # Should NOT have exited
        mock_exit.assert_not_called()
        assert lm.state == LifecycleState.WORKING

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_idle_timeout_posts_child_timeout_notification(self, mock_exit):
        captured: dict[str, object] = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Response()

        lm = PerTaskLifecycleManager(agent_id="test", idle_timeout_seconds=0.1)
        lm.configure_timeout_notification(
            "http://team-lead:8030/tasks/task-123/callbacks",
            orchestrator_task_id="task-123",
        )

        with patch("framework.lifecycle.urlopen", side_effect=fake_urlopen):
            lm.arm_idle_timer("child-task-1")
            time.sleep(0.3)

        assert captured["url"] == "http://team-lead:8030/tasks/task-123/child-timeout"
        assert captured["body"]["childTaskId"] == "child-task-1"
        assert captured["body"]["childAgentId"] == "test"
        assert captured["body"]["orchestratorTaskId"] == "task-123"
        mock_exit.assert_called_once_with(EXIT_IDLE_TIMEOUT, reason="idle timeout")


class TestWorkflowIntegration:
    """Test typical lifecycle flows."""

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_full_flow_work_then_ack(self, mock_exit):
        """Simulate: task arrives → work → complete → idle → ACK → exit."""
        lm = PerTaskLifecycleManager(agent_id="web-dev", idle_timeout_seconds=10)

        # New task arrives
        lm.cancel_idle_timer()
        lm.mark_working("task-1")
        assert lm.state == LifecycleState.WORKING

        # Task completes
        lm.arm_idle_timer("task-1")
        assert lm.state == LifecycleState.IDLE_WAITING

        # Parent sends ACK
        lm.handle_ack("task-1")
        assert lm.state == LifecycleState.SHUTTING_DOWN
        mock_exit.assert_called_once_with(EXIT_ACK, reason="ACK received for task task-1")

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_revision_cycle(self, mock_exit):
        """Simulate: work → idle → new task (revision) → work → idle → ACK."""
        lm = PerTaskLifecycleManager(agent_id="web-dev", idle_timeout_seconds=10)

        # First task
        lm.mark_working("task-1")
        lm.arm_idle_timer("task-1")
        assert lm.state == LifecycleState.IDLE_WAITING

        # Revision arrives (new task reuses container)
        lm.cancel_idle_timer()
        lm.mark_working("task-2")
        assert lm.state == LifecycleState.WORKING

        # Second task completes
        lm.arm_idle_timer("task-2")

        # ACK for final task
        lm.handle_ack("task-2")
        assert lm.state == LifecycleState.SHUTTING_DOWN
        mock_exit.assert_called_once()
