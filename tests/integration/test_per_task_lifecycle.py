"""Integration tests for per-task agent lifecycle management.

Tests two critical paths:
  1. Normal reuse — Team Lead reuses the same dev agent container for revision cycles
  2. Replacement after exit — Team Lead launches a replacement when the dev agent is dead

These tests do NOT require Docker or external services.
They use in-process agent instances to verify lifecycle flows end-to-end.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.lifecycle import (
    EXIT_ACK,
    EXIT_IDLE_TIMEOUT,
    EXIT_TERMINATE,
    LifecycleState,
    PerTaskLifecycleManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lifecycle(timeout: float = 10.0) -> PerTaskLifecycleManager:
    return PerTaskLifecycleManager(agent_id="web-dev", idle_timeout_seconds=timeout)


# ---------------------------------------------------------------------------
# Path 1: Normal reuse
# ---------------------------------------------------------------------------

class TestNormalReuseLifecycle:
    """Container is reused for revision cycles; ACK is sent at the very end."""

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_container_reuse_for_revision(self, mock_exit):
        """First task completes, revision arrives before idle timeout — no exit."""
        lm = _make_lifecycle(timeout=1.0)

        # Round 1: initial implementation
        lm.mark_working("task-1")
        assert lm.state == LifecycleState.WORKING

        lm.arm_idle_timer("task-1")
        assert lm.state == LifecycleState.IDLE_WAITING

        # Revision arrives quickly (before timeout)
        lm.cancel_idle_timer()
        lm.mark_working("task-2")
        assert lm.state == LifecycleState.WORKING

        # Should NOT have exited yet
        mock_exit.assert_not_called()

        # Round 2 completes
        lm.arm_idle_timer("task-2")
        assert lm.state == LifecycleState.IDLE_WAITING

        # Team Lead sends ACK
        lm.handle_ack("task-2")
        assert lm.state == LifecycleState.SHUTTING_DOWN
        mock_exit.assert_called_once_with(EXIT_ACK, reason="ACK received for task task-2")

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_multiple_revision_cycles(self, mock_exit):
        """Three revision rounds before final ACK."""
        lm = _make_lifecycle(timeout=1.0)

        for i in range(1, 4):
            lm.cancel_idle_timer()
            lm.mark_working(f"task-{i}")
            lm.arm_idle_timer(f"task-{i}")

        # ACK after round 3
        lm.handle_ack("task-3")
        assert lm.state == LifecycleState.SHUTTING_DOWN
        mock_exit.assert_called_once_with(EXIT_ACK, reason="ACK received for task task-3")

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_ack_received_during_idle_cancels_timer(self, mock_exit):
        """Timer is cancelled when ACK arrives in IDLE_WAITING state."""
        lm = _make_lifecycle(timeout=5.0)
        lm.arm_idle_timer("task-1")
        assert lm._timer is not None

        lm.handle_ack("task-1")

        assert lm._timer is None
        assert lm.state == LifecycleState.SHUTTING_DOWN

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_ping_extends_lifetime_during_long_llm_call(self, mock_exit):
        """Pings reset idle timer while Team Lead does long LLM analysis."""
        lm = _make_lifecycle(timeout=0.3)
        lm.arm_idle_timer("task-1")

        # Send pings to prevent timeout
        for _ in range(3):
            time.sleep(0.1)
            result = lm.handle_ping("task-1")
            assert result["status"] == "ok"
            assert "timer reset" in result["message"]

        # Timeout should NOT have fired yet
        mock_exit.assert_not_called()

        # Now let it expire
        time.sleep(0.5)
        mock_exit.assert_called_once_with(EXIT_IDLE_TIMEOUT, reason="idle timeout")


# ---------------------------------------------------------------------------
# Path 2: Replacement after abnormal exit
# ---------------------------------------------------------------------------

class TestReplacementAfterExit:
    """Team Lead must detect a dead container and launch a replacement."""

    def test_idle_timeout_fires_with_exit_code_2(self):
        """Idle timeout exits with code 2 (EXIT_IDLE_TIMEOUT)."""
        exit_code_captured = []
        notify_captured = []

        def fake_exit(code: int):
            exit_code_captured.append(code)

        def notify_timeout(task_id: str):
            notify_captured.append(task_id)

        lm = PerTaskLifecycleManager(
            agent_id="web-dev",
            idle_timeout_seconds=0.1,
            on_timeout_notify=notify_timeout,
        )

        with patch.object(lm, "_schedule_exit", side_effect=lambda code, **_: fake_exit(code)):
            lm.arm_idle_timer("task-1")
            time.sleep(0.3)

        assert exit_code_captured == [EXIT_IDLE_TIMEOUT]
        assert notify_captured == ["task-1"]

    def test_terminate_fires_with_exit_code_1(self):
        """Terminate handler fires with exit code 1 (EXIT_TERMINATE)."""
        exit_code_captured = []

        lm = PerTaskLifecycleManager(agent_id="web-dev", idle_timeout_seconds=10)
        with patch.object(lm, "_schedule_exit", side_effect=lambda code, **_: exit_code_captured.append(code)):
            lm.mark_working("task-1")
            result = lm.handle_terminate("task-1")

        assert result["status"] == "ok"
        assert "terminating" in result["message"]
        assert exit_code_captured == [EXIT_TERMINATE]
        assert lm.state == LifecycleState.SHUTTING_DOWN

    @patch("framework.lifecycle.PerTaskLifecycleManager._schedule_exit")
    def test_ack_after_timeout_is_idempotent(self, mock_exit):
        """If timeout already fired, a late ACK is accepted but no double exit."""
        lm = PerTaskLifecycleManager(agent_id="web-dev", idle_timeout_seconds=0.1)
        lm.arm_idle_timer("task-1")

        # Wait for timeout
        time.sleep(0.3)
        assert lm.state == LifecycleState.SHUTTING_DOWN

        # Late ACK arrives
        result = lm.handle_ack("task-1")
        # Should not raise, should not schedule a second exit
        # (already in SHUTTING_DOWN, so ACK is accepted idempotently for this task_id)
        assert result["status"] == "ok"

    def test_web_dev_replacement_launches_after_old_instance_exits(self, monkeypatch):
        """Unreachable Dev child is replaced only after Team Lead confirms the old container is gone."""
        from agents.team_lead.tools import DispatchWebDev

        calls = {"launch": [], "dispatch": []}

        class StubRegistryClient:
            def discover(self, capability):
                return ""

            def get_capability_definition(self, capability):
                return {
                    "agent_id": "web-dev",
                    "execution_mode": "per-task",
                    "launch_spec": {"image": "constellation-v2-web-dev:latest", "port": 8050},
                }

        class StubLauncher:
            def __init__(self):
                self._find_calls = 0

            def find_live_instances(self, agent_id, task_id):
                self._find_calls += 1
                if self._find_calls == 1:
                    return [{
                        "container_name": "web-dev-task-123-old",
                        "task_id": "task-123",
                        "agent_id": "web-dev",
                    }]
                return []

            def launch_instance(self, definition, task_id, launch_overrides=None):
                calls["launch"].append(task_id)
                return {
                    "service_url": "http://replacement-web-dev:8050",
                    "container_name": "web-dev-task-123-new",
                }

            def destroy_instance(self, agent_id, container_name):
                return None

        stub_launcher = StubLauncher()

        def _dispatch_sync(**kwargs):
            calls["dispatch"].append(kwargs)
            if kwargs["url"] == "http://web-dev-task-123-old:8050":
                raise RuntimeError("old instance unreachable")
            return {
                "task": {
                    "id": "child-web-dev-2",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {
                            "parts": [{"text": "replacement complete"}],
                            "metadata": {
                                "prUrl": "https://example.test/pr/2",
                                "prNumber": 2,
                                "repoUrl": "https://example.test/org/repo.git",
                                "branch": "feature/task-123",
                                "changedFiles": ["src/App.tsx"],
                                "jiraInReview": True,
                                "screenshotIncluded": True,
                                "screenshotUploaded": True,
                            },
                        }
                    ],
                }
            }

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("framework.launcher_dispatch.get_launcher", lambda: stub_launcher)
        monkeypatch.setattr("framework.launcher_dispatch.wait_for_agent_ready", lambda *args, **kwargs: None)
        monkeypatch.setattr("agents.team_lead.tools.time.sleep", lambda *args, **kwargs: None)
        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)
        monkeypatch.setenv("TEAM_LEAD_CHILD_REPLACEMENT_CONFIRM_SECONDS", "1")

        result = DispatchWebDev().execute_sync(
            task_description="Implement task",
            orchestrator_task_id="task-123",
            child_service_url="http://web-dev-task-123-old:8050",
            child_container_name="web-dev-task-123-old",
        )

        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert calls["launch"] == ["task-123"]
        assert calls["dispatch"][-1]["url"] == "http://replacement-web-dev:8050"

    def test_code_review_replacement_launches_after_old_instance_exits(self, monkeypatch):
        """Unreachable Code Review child is replaced only after the old container is confirmed gone."""
        from agents.team_lead.tools import DispatchCodeReview

        calls = {"launch": [], "dispatch": []}

        class StubRegistryClient:
            def discover(self, capability):
                return ""

            def get_capability_definition(self, capability):
                return {
                    "agent_id": "code-review",
                    "execution_mode": "per-task",
                    "launch_spec": {"image": "constellation-v2-code-review:latest", "port": 8060},
                }

        class StubLauncher:
            def __init__(self):
                self._find_calls = 0

            def find_live_instances(self, agent_id, task_id):
                self._find_calls += 1
                if self._find_calls == 1:
                    return [{
                        "container_name": "code-review-task-123-old",
                        "task_id": "task-123",
                        "agent_id": "code-review",
                    }]
                return []

            def launch_instance(self, definition, task_id, launch_overrides=None):
                calls["launch"].append(task_id)
                return {
                    "service_url": "http://replacement-code-review:8060",
                    "container_name": "code-review-task-123-new",
                }

            def destroy_instance(self, agent_id, container_name):
                return None

        stub_launcher = StubLauncher()

        def _dispatch_sync(**kwargs):
            calls["dispatch"].append(kwargs)
            if kwargs["url"] == "http://code-review-task-123-old:8060":
                raise RuntimeError("old instance unreachable")
            return {
                "task": {
                    "id": "child-code-review-2",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {
                            "parts": [{"text": json.dumps({"verdict": "approved", "summary": "ok"})}],
                            "metadata": {"agentId": "code-review"},
                        }
                    ],
                }
            }

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("framework.launcher_dispatch.get_launcher", lambda: stub_launcher)
        monkeypatch.setattr("framework.launcher_dispatch.wait_for_agent_ready", lambda *args, **kwargs: None)
        monkeypatch.setattr("agents.team_lead.tools.time.sleep", lambda *args, **kwargs: None)
        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)
        monkeypatch.setenv("TEAM_LEAD_CHILD_REPLACEMENT_CONFIRM_SECONDS", "1")

        result = DispatchCodeReview().execute_sync(
            pr_url="https://example.test/pr/7",
            orchestrator_task_id="task-123",
            task_id="task-123",
            child_service_url="http://code-review-task-123-old:8060",
            child_container_name="code-review-task-123-old",
        )

        payload = json.loads(result.output)
        assert payload["verdict"] == "approved"
        assert calls["launch"] == ["task-123"]
        assert calls["dispatch"][-1]["url"] == "http://replacement-code-review:8060"

    def test_web_dev_replacement_refuses_duplicate_when_old_instance_stays_live(self, monkeypatch):
        """Team Lead must refuse replacement if the old Dev container is still live."""
        from agents.team_lead.tools import DispatchWebDev

        calls = {"launch": []}

        class StubRegistryClient:
            def discover(self, capability):
                return ""

            def get_capability_definition(self, capability):
                return {
                    "agent_id": "web-dev",
                    "execution_mode": "per-task",
                    "launch_spec": {"image": "constellation-v2-web-dev:latest", "port": 8050},
                }

        class StubLauncher:
            def find_live_instances(self, agent_id, task_id):
                return [{
                    "container_name": "web-dev-task-123-old",
                    "task_id": "task-123",
                    "agent_id": "web-dev",
                }]

            def launch_instance(self, definition, task_id, launch_overrides=None):
                calls["launch"].append(task_id)
                raise AssertionError("replacement launch should not occur while old instance is still live")

            def destroy_instance(self, agent_id, container_name):
                return None

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("framework.launcher_dispatch.get_launcher", lambda: StubLauncher())
        monkeypatch.setattr("agents.team_lead.tools.get_launcher", lambda: StubLauncher())
        monkeypatch.setattr("agents.team_lead.tools.time.sleep", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "framework.a2a.client.dispatch_sync",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("old instance unreachable")),
        )
        monkeypatch.setenv("TEAM_LEAD_CHILD_REPLACEMENT_CONFIRM_SECONDS", "0")

        result = DispatchWebDev().execute_sync(
            task_description="Implement task",
            orchestrator_task_id="task-123",
            child_service_url="http://web-dev-task-123-old:8050",
            child_container_name="web-dev-task-123-old",
        )

        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "still live but unreachable" in payload["message"]
        assert calls["launch"] == []


# ---------------------------------------------------------------------------
# Path 3: Duplicate instance prevention
# ---------------------------------------------------------------------------

class TestDuplicateInstancePrevention:
    """Launcher.find_live_instances() prevents duplicate launches."""

    def test_find_live_instances_empty_when_no_containers(self):
        """Returns empty list when Docker has no matching containers."""
        from framework.launcher import Launcher

        lm = Launcher.__new__(Launcher)
        with patch.object(lm, "_request_raw", return_value=(200, json.dumps([]))):
            result = lm.find_live_instances("web-dev", "task-123")
        assert result == []

    def test_find_live_instances_returns_running_containers(self):
        """Returns running container info when found."""
        from framework.launcher import Launcher

        fake_response = json.dumps([
            {
                "Names": ["/web-dev-task-123-abc12345"],
                "Labels": {
                    "constellation.agent_id": "web-dev",
                    "constellation.task_id": "task-123",
                },
                "Status": "running",
            }
        ])

        lm = Launcher.__new__(Launcher)
        with patch.object(lm, "_request_raw", return_value=(200, fake_response)):
            result = lm.find_live_instances("web-dev", "task-123")

        assert len(result) == 1
        assert result[0]["agent_id"] == "web-dev"
        assert result[0]["task_id"] == "task-123"
        assert result[0]["container_name"] == "web-dev-task-123-abc12345"

    def test_find_live_instances_handles_docker_error_gracefully(self):
        """Returns empty list (non-fatal) when Docker API fails."""
        from framework.launcher import Launcher

        lm = Launcher.__new__(Launcher)
        with patch.object(lm, "_request_raw", side_effect=RuntimeError("socket error")):
            result = lm.find_live_instances("web-dev", "task-123")
        assert result == []


# ---------------------------------------------------------------------------
# Path 4: Dual ACK (Dev + CR)
# ---------------------------------------------------------------------------

class TestDualAck:
    """Team Lead sends ACK to both Dev and CR agents simultaneously."""

    @pytest.mark.asyncio
    async def test_ack_sent_to_both_dev_and_cr(self):
        """_ack_and_cleanup_dev_agent sends ACK to dev if dev session present."""
        from agents.team_lead.nodes import _ack_and_cleanup_dev_agent

        ack_calls: list[str] = []

        async def _capture_ack(url, tid, exit_reason="task_completed_success", orchestrator_task_id=""):
            ack_calls.append(f"{url}/{tid}")

        mock_client = AsyncMock()
        mock_client.send_ack = _capture_ack

        state = {
            "_task_id": "orchestrator-task-1",
            "dev_agent_session": {
                "task_id": "dev-task-1",
                "service_url": "http://web-dev:8000",
                "container_name": "web-dev-task-1-abc",
                "agent_id": "web-dev",
            },
            "cr_agent_session": {
                "task_id": "cr-task-1",
                "service_url": "http://code-review:8000",
                "container_name": "code-review-task-1-abc",
                "agent_id": "code-review",
            },
        }

        with patch("framework.a2a.client.A2AClient", return_value=mock_client):
            with patch("framework.launcher.get_launcher") as mock_launcher:
                mock_launcher.return_value.destroy_instance = MagicMock()
                result = await _ack_and_cleanup_dev_agent(state)

        assert result["dev_agent_acknowledged"] is True
        assert result["cr_agent_acknowledged"] is True
        assert result["dev_agent_session"] == {}
        assert result["cr_agent_session"] == {}
        # Both ACKs were sent
        assert any("http://web-dev:8000/dev-task-1" in c for c in ack_calls)
        assert any("http://code-review:8000/cr-task-1" in c for c in ack_calls)

    @pytest.mark.asyncio
    async def test_ack_tolerates_missing_cr_session(self):
        """Works correctly when no CR session exists (first dispatch, no reviews yet)."""
        from agents.team_lead.nodes import _ack_and_cleanup_dev_agent

        async def _capture_ack(url, tid, exit_reason="task_completed_success", orchestrator_task_id=""):
            pass

        mock_client = AsyncMock()
        mock_client.send_ack = _capture_ack

        state = {
            "_task_id": "orchestrator-task-1",
            "dev_agent_session": {
                "task_id": "dev-task-1",
                "service_url": "http://web-dev:8000",
                "container_name": "web-dev-task-1-abc",
                "agent_id": "web-dev",
            },
            # No cr_agent_session
        }

        with patch("framework.a2a.client.A2AClient", return_value=mock_client):
            with patch("framework.launcher.get_launcher") as mock_launcher:
                mock_launcher.return_value.destroy_instance = MagicMock()
                result = await _ack_and_cleanup_dev_agent(state)

        assert result["dev_agent_acknowledged"] is True
        assert result.get("cr_agent_acknowledged") is False  # no CR session
        assert result["dev_agent_session"] == {}
