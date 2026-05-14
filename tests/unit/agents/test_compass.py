"""Tests for Compass Agent (LLM-driven ReAct)."""
import json
import pytest
from unittest.mock import MagicMock
from agents.compass.agent import CompassAgent, compass_definition
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
    return CompassAgent(definition=compass_definition, services=services)


def _mock_runtime(summary="Task dispatched.", success=True):
    result = MagicMock()
    result.success = success
    result.summary = summary
    runtime = MagicMock()
    runtime.run_agentic.return_value = result
    return runtime


class TestCompassDefinition:
    def test_agent_id(self):
        assert compass_definition.agent_id == "compass"

    def test_mode(self):
        assert compass_definition.mode == AgentMode.CHAT

    def test_execution_mode(self):
        assert compass_definition.execution_mode == ExecutionMode.PERSISTENT

    def test_no_workflow(self):
        assert compass_definition.workflow is None

    def test_has_tools(self):
        assert len(compass_definition.tools) > 0


class TestCompassAgent:
    async def test_handles_development_task(self):
        runtime = _mock_runtime("Development task dispatched to Team Lead.")
        agent = _make_agent(runtime)

        message = {
            "parts": [{"text": "Fix bug in Jira ticket ABC-123"}],
            "metadata": {},
        }
        result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        artifacts = result["task"]["artifacts"]
        assert any("dispatched" in a["parts"][0]["text"].lower() for a in artifacts)
        runtime.run_agentic.assert_called_once()

    async def test_handles_office_task(self):
        runtime = _mock_runtime("Office document summarized.")
        agent = _make_agent(runtime)

        message = {
            "parts": [{"text": "Summarize the PDF in my documents folder"}],
            "metadata": {},
        }
        result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        runtime.run_agentic.assert_called_once()

    async def test_failed_result_maps_to_failed_state(self):
        runtime = _mock_runtime("Something went wrong.", success=False)
        agent = _make_agent(runtime)

        message = {"parts": [{"text": "Do something"}], "metadata": {}}
        result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_FAILED"

    async def test_get_task_nonexistent_returns_failed(self):
        """Non-existent task ID returns FAILED from TaskStore."""
        agent = _make_agent(MagicMock())
        result = await agent.get_task("task-001")
        assert result["task"]["status"]["state"] == "TASK_STATE_FAILED"

    async def test_get_task_returns_real_state(self):
        """After handle_message, get_task returns real completed state."""
        runtime = _mock_runtime("Done.")
        agent = _make_agent(runtime)

        message = {"parts": [{"text": "Hello"}], "metadata": {}}
        result = await agent.handle_message(message)
        task_id = result["task"]["id"]

        poll = await agent.get_task(task_id)
        assert poll["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    async def test_handle_message_accepts_a2a_envelope(self):
        runtime = _mock_runtime("Development task dispatched to Team Lead.")
        agent = _make_agent(runtime)

        message = {
            "message": {
                "parts": [{"text": "Implement PROJ-123"}],
                "metadata": {},
            }
        }
        result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        runtime.run_agentic.assert_called_once()


class TestCompassTools:
    def test_dispatch_development_task_prefers_registry_discovery(self, monkeypatch):
        from agents.compass.tools import DispatchDevelopmentTask

        class StubRegistryClient:
            def discover(self, capability):
                assert capability == "team-lead.task.analyze"
                return "http://registry-team-lead:8030"

        dispatched = {}

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )

        def _dispatch_sync(url, capability, message_parts, metadata, **kwargs):
            dispatched["url"] = url
            dispatched["capability"] = capability
            dispatched["metadata"] = metadata
            return {
                "task": {
                    "artifacts": [
                        {
                            "parts": [{"text": "Task completed."}],
                            "metadata": {},
                        }
                    ]
                }
            }

        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        result = DispatchDevelopmentTask().execute_sync(
            task_description="Implement PROJ-123",
            jira_key="PROJ-123",
        )

        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert dispatched["url"] == "http://registry-team-lead:8030"
        assert dispatched["capability"] == "team-lead.task.analyze"
        assert dispatched["metadata"]["jiraKey"] == "PROJ-123"

    def test_dispatch_development_task_propagates_failed_team_lead_state(self, monkeypatch):
        from agents.compass.tools import DispatchDevelopmentTask

        class StubRegistryClient:
            def discover(self, capability):
                return "http://registry-team-lead:8030"

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
                        "message": {"parts": [{"text": "Jira ticket not accessible: PROJ-123"}]},
                    },
                    "artifacts": [],
                }
            },
        )

        result = DispatchDevelopmentTask().execute_sync(task_description="Implement PROJ-123")
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert payload["state"] == "TASK_STATE_FAILED"
        assert "Jira ticket not accessible" in payload["message"]
