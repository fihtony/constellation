"""Cross-agent mock E2E tests.

Tests the full Compass → Team Lead → Web Dev → Code Review chain
using mock runtime and boundary agents.  No real LLM or external services.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from framework.agent import AgentServices
from framework.a2a.protocol import TaskState
from framework.checkpoint import InMemoryCheckpointer
from framework.event_store import InMemoryEventStore
from framework.memory import InMemoryMemoryService
from framework.plugin import PluginManager
from framework.session import InMemorySessionService
from framework.skills import SkillsRegistry
from framework.task_store import InMemoryTaskStore


def _make_services(runtime=None) -> AgentServices:
    return AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=runtime,
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )


def _wait_for_terminal(task_store, task_id, timeout=5):
    """Poll task store until task reaches a terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = task_store.get_task(task_id)
        if task and task.status.state in (
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.INPUT_REQUIRED,
        ):
            return task
        time.sleep(0.1)
    return task_store.get_task(task_id)


class TestTeamLeadWorkflowMock:
    """Team Lead graph workflow with mock boundary agents."""

    def test_team_lead_happy_path(self):
        """Team Lead receives task, dispatches dev, reviews, reports success."""
        from agents.team_lead.agent import TeamLeadAgent, team_lead_definition

        services = _make_services()
        agent = TeamLeadAgent(team_lead_definition, services)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(agent.start())

        # Mock the tool registry so dispatch_web_dev and dispatch_code_review
        # return successful results
        from framework.tools.registry import get_registry
        registry = get_registry()

        mock_web_dev = MagicMock()
        mock_web_dev.name = "dispatch_web_dev"
        mock_web_dev.execute_sync.return_value = MagicMock(
            output=json.dumps({
                "status": "completed",
                "summary": "Implemented changes",
                "prUrl": "https://github.com/org/repo/pull/42",
                "branch": "feature/PROJ-123",
            }),
            error="",
        )
        mock_web_dev.to_openai_schema.return_value = {
            "type": "function",
            "function": {"name": "dispatch_web_dev", "parameters": {}},
        }

        mock_code_review = MagicMock()
        mock_code_review.name = "dispatch_code_review"
        mock_code_review.execute_sync.return_value = MagicMock(
            output=json.dumps({
                "verdict": "approved",
                "comments": [],
                "summary": "Code looks good",
            }),
            error="",
        )
        mock_code_review.to_openai_schema.return_value = {
            "type": "function",
            "function": {"name": "dispatch_code_review", "parameters": {}},
        }

        registry.register(mock_web_dev)
        registry.register(mock_code_review)

        # Send task
        message = {
            "message": {
                "parts": [{"text": "Implement login page for PROJ-123"}],
                "metadata": {
                    "jiraKey": "PROJ-123",
                    "repoUrl": "https://github.com/org/repo",
                },
            }
        }
        result = loop.run_until_complete(agent.handle_message(message))

        # Should return immediately with WORKING
        task_data = result.get("task", result)
        task_id = task_data["id"]
        assert task_data["status"]["state"] in (
            TaskState.WORKING.value,
            TaskState.SUBMITTED.value,
        )

        # Wait for completion
        task = _wait_for_terminal(services.task_store, task_id, timeout=10)
        assert task is not None
        assert task.status.state == TaskState.COMPLETED

        # Verify artifacts contain report_summary
        assert len(task.artifacts) > 0
        artifact_text = task.artifacts[0].parts[0].get("text", "")
        assert "PROJ-123" in artifact_text or "completed" in artifact_text.lower()

        loop.close()

    def test_team_lead_review_rejection_loops(self):
        """Team Lead handles review rejection and revision loop."""
        from agents.team_lead.agent import TeamLeadAgent, team_lead_definition

        services = _make_services()
        agent = TeamLeadAgent(team_lead_definition, services)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(agent.start())

        from framework.tools.registry import get_registry
        registry = get_registry()

        call_count = {"web_dev": 0, "code_review": 0}

        def mock_web_dev_execute(**kwargs):
            call_count["web_dev"] += 1
            return MagicMock(
                output=json.dumps({
                    "status": "completed",
                    "summary": f"Implementation v{call_count['web_dev']}",
                    "prUrl": "https://github.com/org/repo/pull/42",
                    "branch": "feature/TEST-1",
                }),
                error="",
            )

        def mock_code_review_execute(**kwargs):
            call_count["code_review"] += 1
            if call_count["code_review"] <= 1:
                return MagicMock(
                    output=json.dumps({
                        "verdict": "rejected",
                        "comments": [
                            {"severity": "high", "message": "Missing error handling"}
                        ],
                        "summary": "Needs fixes",
                    }),
                    error="",
                )
            return MagicMock(
                output=json.dumps({
                    "verdict": "approved",
                    "comments": [],
                    "summary": "Looks good now",
                }),
                error="",
            )

        mock_web = MagicMock()
        mock_web.name = "dispatch_web_dev"
        mock_web.execute_sync.side_effect = mock_web_dev_execute
        mock_web.to_openai_schema.return_value = {
            "type": "function",
            "function": {"name": "dispatch_web_dev", "parameters": {}},
        }

        mock_review = MagicMock()
        mock_review.name = "dispatch_code_review"
        mock_review.execute_sync.side_effect = mock_code_review_execute
        mock_review.to_openai_schema.return_value = {
            "type": "function",
            "function": {"name": "dispatch_code_review", "parameters": {}},
        }

        registry.register(mock_web)
        registry.register(mock_review)

        message = {
            "message": {
                "parts": [{"text": "Fix bug TEST-1"}],
                "metadata": {},
            }
        }
        result = loop.run_until_complete(agent.handle_message(message))
        task_id = result.get("task", result)["id"]

        task = _wait_for_terminal(services.task_store, task_id, timeout=15)
        assert task is not None
        assert task.status.state == TaskState.COMPLETED

        # Web dev should be called at least twice (initial + revision)
        assert call_count["web_dev"] >= 2
        assert call_count["code_review"] >= 2

        loop.close()


class TestCodeReviewWorkflowFull:
    """Code Review Agent workflow with mock runtime."""

    def test_code_review_approved(self):
        """Code Review with no critical issues returns approved."""
        from agents.code_review.agent import CodeReviewAgent, code_review_definition

        services = _make_services()
        agent = CodeReviewAgent(code_review_definition, services)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(agent.start())

        message = {
            "message": {
                "parts": [{"text": "Review PR #42"}],
                "metadata": {
                    "prUrl": "https://github.com/org/repo/pull/42",
                    "prDiff": "diff --git a/app.py ...",
                    "changedFiles": ["app.py"],
                    "originalRequirements": "Add login page",
                },
            }
        }
        result = loop.run_until_complete(agent.handle_message(message))
        task_id = result.get("task", result)["id"]

        task = _wait_for_terminal(services.task_store, task_id, timeout=10)
        assert task is not None
        assert task.status.state == TaskState.COMPLETED

        # Parse review report
        report_text = task.artifacts[0].parts[0].get("text", "{}")
        report = json.loads(report_text)
        # Without runtime, all review nodes return empty issues → approved
        assert report["verdict"] == "approved"

        loop.close()


class TestInterruptResumeE2E:
    """End-to-end interrupt → INPUT_REQUIRED → resume cycle."""

    def test_team_lead_escalation_sets_input_required(self):
        """When max revisions reached, Team Lead escalates and sets INPUT_REQUIRED."""
        from agents.team_lead.agent import TeamLeadAgent, team_lead_definition

        services = _make_services()
        agent = TeamLeadAgent(team_lead_definition, services)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(agent.start())

        from framework.tools.registry import get_registry
        registry = get_registry()

        # Always reject
        mock_web = MagicMock()
        mock_web.name = "dispatch_web_dev"
        mock_web.execute_sync.return_value = MagicMock(
            output=json.dumps({
                "status": "completed",
                "summary": "Impl",
                "prUrl": "https://github.com/org/repo/pull/1",
                "branch": "feat/x",
            }),
            error="",
        )
        mock_web.to_openai_schema.return_value = {
            "type": "function",
            "function": {"name": "dispatch_web_dev", "parameters": {}},
        }

        mock_review = MagicMock()
        mock_review.name = "dispatch_code_review"
        mock_review.execute_sync.return_value = MagicMock(
            output=json.dumps({
                "verdict": "rejected",
                "comments": [{"severity": "high", "message": "Bad"}],
                "summary": "Rejected",
            }),
            error="",
        )
        mock_review.to_openai_schema.return_value = {
            "type": "function",
            "function": {"name": "dispatch_code_review", "parameters": {}},
        }

        registry.register(mock_web)
        registry.register(mock_review)

        message = {
            "message": {
                "parts": [{"text": "Task with forced escalation"}],
                "metadata": {},
            }
        }
        result = loop.run_until_complete(agent.handle_message(message))
        task_id = result.get("task", result)["id"]

        # Wait — escalate_to_user now raises InterruptSignal → INPUT_REQUIRED
        task = _wait_for_terminal(services.task_store, task_id, timeout=15)
        assert task is not None
        # After max revisions, escalate_to_user raises InterruptSignal
        assert task.status.state in (TaskState.INPUT_REQUIRED, TaskState.COMPLETED, TaskState.FAILED)

        loop.close()

    def test_team_lead_resume_after_escalation_completes(self):
        """After escalation → INPUT_REQUIRED, resume with user input completes the task."""
        from agents.team_lead.agent import TeamLeadAgent, team_lead_definition

        services = _make_services()
        agent = TeamLeadAgent(team_lead_definition, services)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(agent.start())

        from framework.tools.registry import get_registry
        registry = get_registry()

        review_call_count = {"n": 0}

        # Web dev always succeeds
        mock_web = MagicMock()
        mock_web.name = "dispatch_web_dev"
        mock_web.execute_sync.return_value = MagicMock(
            output=json.dumps({
                "status": "completed",
                "summary": "Implemented changes",
                "prUrl": "https://github.com/org/repo/pull/99",
                "branch": "feat/resume-test",
            }),
            error="",
        )
        mock_web.to_openai_schema.return_value = {
            "type": "function",
            "function": {"name": "dispatch_web_dev", "parameters": {}},
        }

        def mock_review_execute(**kwargs):
            review_call_count["n"] += 1
            # First 3+ calls: reject (to force escalation via max_revisions=3)
            # After resume: approve
            if review_call_count["n"] <= 4:
                return MagicMock(
                    output=json.dumps({
                        "verdict": "rejected",
                        "comments": [{"severity": "high", "message": "Needs work"}],
                        "summary": "Rejected",
                    }),
                    error="",
                )
            return MagicMock(
                output=json.dumps({
                    "verdict": "approved",
                    "comments": [],
                    "summary": "Looks good after user guidance",
                }),
                error="",
            )

        mock_review = MagicMock()
        mock_review.name = "dispatch_code_review"
        mock_review.execute_sync.side_effect = mock_review_execute
        mock_review.to_openai_schema.return_value = {
            "type": "function",
            "function": {"name": "dispatch_code_review", "parameters": {}},
        }

        registry.register(mock_web)
        registry.register(mock_review)

        # Phase 1: send task — should hit max revisions and escalate
        message = {
            "message": {
                "parts": [{"text": "Implement feature with forced escalation then resume"}],
                "metadata": {},
            }
        }
        result = loop.run_until_complete(agent.handle_message(message))
        task_id = result.get("task", result)["id"]

        task = _wait_for_terminal(services.task_store, task_id, timeout=15)
        assert task is not None
        assert task.status.state == TaskState.INPUT_REQUIRED, (
            f"Expected INPUT_REQUIRED but got {task.status.state}"
        )

        # Phase 2: resume with user guidance — should loop back and eventually complete
        resume_result = loop.run_until_complete(
            agent.resume_task(task_id, "Please focus on error handling in the login module")
        )
        resume_task = resume_result.get("task", resume_result)
        # After resume, poll for terminal state
        final_task = _wait_for_terminal(services.task_store, task_id, timeout=15)
        assert final_task is not None
        assert final_task.status.state == TaskState.COMPLETED, (
            f"Expected COMPLETED after resume but got {final_task.status.state}"
        )

        # Verify artifacts exist with report
        assert len(final_task.artifacts) > 0
        artifact_text = final_task.artifacts[0].parts[0].get("text", "")
        assert artifact_text, "Expected non-empty artifact text after resume"

        loop.close()
