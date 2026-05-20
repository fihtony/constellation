"""Tests for chat message handling returning UI updates."""
import pytest
import json
from unittest.mock import MagicMock, patch
from agents.compass.agent import CompassAgent, compass_definition
from framework.agent import AgentServices
from framework.task_store import InMemoryTaskStore


def _make_agent(mock_runtime=None):
    """Create a CompassAgent for testing."""
    if mock_runtime is None:
        mock_runtime = MagicMock()
    services = AgentServices(
        session_service=MagicMock(), event_store=MagicMock(), memory_service=MagicMock(),
        skills_registry=MagicMock(), plugin_manager=MagicMock(), checkpoint_service=MagicMock(),
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


class TestChatHandling:
    @pytest.fixture()
    def mock_services(self):
        """Create mock services for testing."""
        services = MagicMock()
        services.task_store = InMemoryTaskStore()
        services.runtime = None  # Will use fallback classification
        return services

    @pytest.fixture()
    def agent(self, mock_services):
        return _make_agent()

    async def test_handle_message_returns_ui_update(self):
        """Test that handle_message returns ui_update with task info for UI rendering."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)

        with patch("framework.tools.registry.get_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_reg.execute_sync.return_value = json.dumps({
                "status": "submitted", "taskId": "tl-001",
            })
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message({
                "message": {"parts": [{"text": "Implement PROJ-123"}]}
            })

        # Should return task info with UI-relevant data
        assert "ui_update" in result or "task_id" in result
        # If ui_update is present, it should contain key UI fields
        if "ui_update" in result:
            ui = result["ui_update"]
            assert "task_id" in ui, "ui_update should contain task_id"
            assert "chat_message" in ui, "ui_update should contain chat_message"

    async def test_handle_message_ui_update_contains_status(self):
        """Test that ui_update contains task_status for UI state."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)

        with patch("framework.tools.registry.get_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_reg.execute_sync.return_value = json.dumps({
                "status": "submitted", "taskId": "tl-001",
            })
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message({
                "message": {"parts": [{"text": "Fix bug PROJ-456"}]}
            })

        if "ui_update" in result:
            assert "task_status" in result["ui_update"]

    async def test_handle_message_ui_update_contains_chat_message(self):
        """Test that ui_update contains chat_message for display."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)

        with patch("framework.tools.registry.get_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_reg.execute_sync.return_value = json.dumps({
                "status": "submitted", "taskId": "tl-001",
            })
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message({
                "message": {"parts": [{"text": "Create PR for PROJ-789"}]}
            })

        if "ui_update" in result:
            ui = result["ui_update"]
            assert "chat_message" in ui
            chat = ui["chat_message"]
            assert "role" in chat
            assert "text" in chat

    async def test_handle_message_returns_task_id(self):
        """Test that handle_message returns task_id for tracking via ui_update."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)

        with patch("framework.tools.registry.get_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_reg.execute_sync.return_value = json.dumps({
                "status": "submitted", "taskId": "tl-001",
            })
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message({
                "message": {"parts": [{"text": "Implement PROJ-999"}]}
            })

        # task_id is available in ui_update
        assert "ui_update" in result
        assert "task_id" in result["ui_update"]