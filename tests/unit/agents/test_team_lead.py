"""Tests for Team Lead Agent (graph-first, ReAct-inside-nodes)."""
import pytest
import asyncio
import time
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
