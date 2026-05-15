"""Tests for Team Lead Agent (graph-first, ReAct-inside-nodes)."""
import pytest
import asyncio
import time
import json
from unittest.mock import MagicMock
from agents.team_lead.agent import TeamLeadAgent, team_lead_definition
from framework.agent import AgentMode, AgentServices, ExecutionMode


def _make_agent(mock_runtime):
    from unittest.mock import MagicMock as M
    from framework.task_store import InMemoryTaskStore
    services = AgentServices(
        session_service=M(), event_store=M(), memory_service=M(),
        skills_registry=M(), plugin_manager=M(), checkpoint_service=M(),
        runtime=mock_runtime, registry_client=None,
        task_store=InMemoryTaskStore(),
    )
    agent = TeamLeadAgent(definition=team_lead_definition, services=services)
    return agent


def _mock_runtime(summary="PR created.", success=True):
    result = MagicMock()
    result.success = success
    result.summary = summary
    runtime = MagicMock()
    runtime.run_agentic.return_value = result
    runtime.run.return_value = {"raw_response": "{}"}
    return runtime


class TestTeamLeadDefinition:
    def test_agent_id(self):
        assert team_lead_definition.agent_id == "team-lead"

    def test_mode(self):
        assert team_lead_definition.mode == AgentMode.TASK

    def test_execution_mode(self):
        assert team_lead_definition.execution_mode == ExecutionMode.PERSISTENT

    def test_has_workflow(self):
        """Team Lead is now graph-first — it MUST have a workflow."""
        assert team_lead_definition.workflow is not None

    def test_has_tools(self):
        assert len(team_lead_definition.tools) > 0


class TestTeamLeadAgent:
    async def test_handle_message_returns_working(self):
        """handle_message returns immediately with WORKING state (async workflow)."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)
        await agent.start()

        message = {
            "parts": [{"text": "Implement feature ABC-123"}],
            "metadata": {"jiraKey": "ABC-123"},
        }
        result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_WORKING"
        assert result["task"]["id"]

    async def test_get_task_returns_real_state(self):
        """get_task returns real state from TaskStore."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)
        await agent.start()

        message = {
            "parts": [{"text": "Fix bug"}],
            "metadata": {},
        }
        result = await agent.handle_message(message)
        task_id = result["task"]["id"]

        # Give the worker thread a moment to complete
        await asyncio.sleep(0.5)

        poll = await agent.get_task(task_id)
        # Should be either COMPLETED or FAILED (not the old hardcoded WORKING)
        state = poll["task"]["status"]["state"]
        assert state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED")

    async def test_workflow_produces_report_summary(self):
        """Completed workflow should produce a report_summary in artifacts."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)
        await agent.start()

        message = {
            "parts": [{"text": "Build login page"}],
            "metadata": {},
        }
        result = await agent.handle_message(message)
        task_id = result["task"]["id"]

        await asyncio.sleep(0.5)

        poll = await agent.get_task(task_id)
        if poll["task"]["status"]["state"] == "TASK_STATE_COMPLETED":
            artifacts = poll["task"]["artifacts"]
            assert len(artifacts) > 0


class TestGatherContextFailures:
    async def test_gather_context_continues_when_jira_fetch_fails(self, monkeypatch, tmp_path):
        """Jira fetch failure is best-effort — workflow continues without Jira context."""
        from agents.team_lead.nodes import gather_context

        class StubRegistry:
            def execute_sync(self, name, args):
                if name == "fetch_jira_ticket":
                    return json.dumps({"error": "401 unauthorized"})
                return json.dumps({})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        # Should NOT raise — best-effort continuation
        result = await gather_context({
            "jira_key": "PROJ-123",
            "workspace_path": str(tmp_path),
        })
        # jira_context should remain empty/None
        assert not result.get("jira_context")

    async def test_gather_context_continues_when_repo_clone_fails(self, monkeypatch, tmp_path):
        """Repo clone failure is best-effort — workflow continues without clone."""
        from agents.team_lead.nodes import gather_context

        class StubRegistry:
            def execute_sync(self, name, args):
                if name == "clone_repo":
                    return json.dumps({"error": "clone failed"})
                return json.dumps({})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        # Should NOT raise — best-effort continuation
        result = await gather_context({
            "repo_url": "https://example.com/org/repo.git",
            "workspace_path": str(tmp_path),
        })
        # repo_cloned should be False in the manifest
        assert result.get("repo_cloned") is False or result.get("repo_cloned") is None


class TestTeamLeadTools:
    def test_dispatch_web_dev_propagates_failed_task_state(self, monkeypatch):
        from agents.team_lead.tools import DispatchWebDev

        class StubRegistryClient:
            def discover(self, capability):
                return "http://web-dev:8050"

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )

        monkeypatch.setattr(
            "framework.a2a.client.dispatch_sync",
            lambda **kwargs: {
                "task": {
                    "status": {
                        "state": "TASK_STATE_FAILED",
                        "message": {"parts": [{"text": "Web Dev task failed"}]},
                    },
                    "artifacts": [],
                }
            },
        )

        result = DispatchWebDev().execute_sync(task_description="Implement live e2e change")
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert payload["state"] == "TASK_STATE_FAILED"
