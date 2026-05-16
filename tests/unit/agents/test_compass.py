"""Tests for Compass Agent (hybrid heuristic + LLM routing)."""
import json
import pytest
from unittest.mock import MagicMock, patch
from agents.compass.agent import CompassAgent, compass_definition, _classify_request, _extract_jira_key
from framework.agent import AgentMode, AgentServices, ExecutionMode


def _make_agent(mock_runtime, registry_execute_sync=None):
    from unittest.mock import MagicMock as M
    from framework.task_store import InMemoryTaskStore
    services = AgentServices(
        session_service=M(), event_store=M(), memory_service=M(),
        skills_registry=M(), plugin_manager=M(), checkpoint_service=M(),
        runtime=mock_runtime, registry_client=None,
        task_store=InMemoryTaskStore(),
    )
    agent = CompassAgent(definition=compass_definition, services=services)
    return agent


def _mock_runtime(summary="Task dispatched.", success=True):
    result = MagicMock()
    result.success = success
    result.summary = summary
    runtime = MagicMock()
    runtime.run_agentic.return_value = result
    runtime.run.return_value = {"raw_response": "development"}
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


class TestCompassClassification:
    """Unit tests for the heuristic + LLM classification helper."""

    def test_jira_url_with_implement(self):
        assert _classify_request(
            "implement the jira ticket: https://tarch.atlassian.net/browse/CSTL-2", None
        ) == "development"

    def test_jira_key_with_fix(self):
        assert _classify_request("Fix bug in ABC-123", None) == "development"

    def test_jira_key_alone(self):
        # Has a Jira key but no action keyword — falls back to LLM (runtime=None → general)
        result = _classify_request("CSTL-2", None)
        # Without runtime, should still try LLM (fails gracefully) → general
        assert result in ("development", "general")

    def test_office_signal(self):
        assert _classify_request("Summarize the PDF", None) == "office"

    def test_general_question(self):
        assert _classify_request("What is Python?", None) == "general"

    def test_extract_jira_key(self):
        assert _extract_jira_key("implement https://jira.example.com/browse/PROJ-42") == "PROJ-42"
        assert _extract_jira_key("Fix ABC-123 please") == "ABC-123"
        assert _extract_jira_key("no jira here") == ""


class TestCompassAgent:
    async def test_handles_development_task(self):
        """Development tasks are dispatched directly via registry (not run_agentic)."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)

        message = {
            "parts": [{"text": "implement the jira ticket: https://tarch.atlassian.net/browse/CSTL-2"}],
            "metadata": {},
        }
        with patch("framework.tools.registry.get_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_reg.execute_sync.return_value = json.dumps({
                "status": "submitted", "taskId": "tl-001",
            })
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        artifacts = result["task"]["artifacts"]
        assert any("dispatched" in a["parts"][0]["text"].lower() for a in artifacts)
        # Direct dispatch should NOT call run_agentic
        runtime.run_agentic.assert_not_called()
        mock_reg.execute_sync.assert_called_once_with(
            "dispatch_development_task",
            {"task_description": "implement the jira ticket: https://tarch.atlassian.net/browse/CSTL-2", "jira_key": "CSTL-2"},
        )

    async def test_handles_office_task(self):
        """Office tasks are dispatched directly via registry (not run_agentic)."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)

        message = {
            "parts": [{"text": "Summarize the PDF in my documents folder"}],
            "metadata": {},
        }
        with patch("framework.tools.registry.get_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_reg.execute_sync.return_value = json.dumps({"status": "submitted"})
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        runtime.run_agentic.assert_not_called()

    async def test_general_task_uses_run_agentic(self):
        """General/conversational tasks use run_agentic for LLM response."""
        runtime = _mock_runtime("Python is a programming language.")
        # Ensure LLM classification also returns 'general'
        runtime.run.return_value = {"raw_response": "general"}
        agent = _make_agent(runtime)

        message = {"parts": [{"text": "What is Python?"}], "metadata": {}}
        with patch("framework.tools.registry.get_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        runtime.run_agentic.assert_called_once()

    async def test_get_task_nonexistent_returns_failed(self):
        """Non-existent task ID returns FAILED from TaskStore."""
        agent = _make_agent(MagicMock())
        result = await agent.get_task("task-001")
        assert result["task"]["status"]["state"] == "TASK_STATE_FAILED"

    async def test_get_task_returns_real_state(self):
        """After handle_message, get_task returns real completed state."""
        runtime = _mock_runtime("Done.")
        agent = _make_agent(runtime)

        message = {"parts": [{"text": "Hello there"}], "metadata": {}}
        with patch("framework.tools.registry.get_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message(message)
        task_id = result["task"]["id"]

        poll = await agent.get_task(task_id)
        assert poll["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    async def test_handle_message_accepts_a2a_envelope(self):
        """A2A envelope (message.message.parts) is unwrapped correctly."""
        runtime = _mock_runtime("Done.")
        agent = _make_agent(runtime)

        message = {
            "message": {
                "parts": [{"text": "implement the jira ticket https://jira.example.com/browse/PROJ-123"}],
                "metadata": {},
            }
        }
        with patch("framework.tools.registry.get_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_reg.execute_sync.return_value = json.dumps({"status": "submitted", "taskId": "tl-001"})
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        # Development task → direct dispatch, not run_agentic
        runtime.run_agentic.assert_not_called()


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
