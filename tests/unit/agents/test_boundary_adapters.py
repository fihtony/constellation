"""Tests for boundary agent adapters."""
import json

from unittest.mock import MagicMock

import pytest
from framework.agent import AgentMode, ExecutionMode
from framework.agent import AgentServices
from framework.task_store import InMemoryTaskStore
from agents.jira.adapter import jira_definition, JiraAgentAdapter
from agents.scm.adapter import scm_definition, SCMAgentAdapter
from agents.ui_design.adapter import ui_design_definition, UIDesignAgentAdapter


def _make_services():
    mock = MagicMock()
    return AgentServices(
        session_service=mock,
        event_store=mock,
        memory_service=mock,
        skills_registry=mock,
        plugin_manager=mock,
        checkpoint_service=mock,
        runtime=None,
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )


class TestBoundaryAdapterDefinitions:

    def test_jira_definition(self):
        assert jira_definition.agent_id == "jira"
        assert jira_definition.mode == AgentMode.SINGLE_TURN
        assert jira_definition.execution_mode == ExecutionMode.PERSISTENT
        assert jira_definition.workflow is None

    def test_scm_definition(self):
        assert scm_definition.agent_id == "scm"
        assert scm_definition.mode == AgentMode.SINGLE_TURN
        assert scm_definition.execution_mode == ExecutionMode.PERSISTENT
        assert scm_definition.workflow is None

    def test_ui_design_definition(self):
        assert ui_design_definition.agent_id == "ui-design"
        assert ui_design_definition.mode == AgentMode.SINGLE_TURN
        assert ui_design_definition.execution_mode == ExecutionMode.PERSISTENT
        assert ui_design_definition.workflow is None


class TestBoundaryAdapterEnvelopeSupport:
    async def test_jira_adapter_accepts_a2a_envelope(self):
        provider = MagicMock()
        provider.fetch_issue.return_value = ({"key": "PROJ-123"}, 200)
        adapter = JiraAgentAdapter(jira_definition, _make_services(), jira_provider=provider)

        result = await adapter.handle_message({
            "message": {
                "parts": [{"text": "PROJ-123"}],
                "metadata": {"requestedCapability": "jira.ticket.fetch", "ticketKey": "PROJ-123"},
            }
        })

        artifact_text = result["task"]["artifacts"][0]["parts"][0]["text"]
        payload = json.loads(artifact_text)
        assert payload["ticket"]["key"] == "PROJ-123"

    async def test_scm_adapter_accepts_a2a_envelope(self):
        client = MagicMock()
        client.get_repo.return_value = ({"name": "web-ui-test"}, 200)
        adapter = SCMAgentAdapter(scm_definition, _make_services(), scm_client=client)

        result = await adapter.handle_message({
            "message": {
                "parts": [{"text": "PROJ/web-ui-test"}],
                "metadata": {"requestedCapability": "scm.repo.inspect", "project": "PROJ", "repo": "web-ui-test"},
            }
        })

        artifact_text = result["task"]["artifacts"][0]["parts"][0]["text"]
        payload = json.loads(artifact_text)
        assert payload["repo"]["name"] == "web-ui-test"

    async def test_ui_design_adapter_accepts_a2a_envelope(self):
        figma = MagicMock()
        figma.get_file.return_value = ({"name": "Design", "document": {"children": []}, "lastModified": "now"}, 200)
        adapter = UIDesignAgentAdapter(ui_design_definition, _make_services(), figma_client=figma)

        result = await adapter.handle_message({
            "message": {
                "parts": [{"text": "https://www.figma.com/design/file-id/mock"}],
                "metadata": {"requestedCapability": "figma.file.fetch", "figmaUrl": "https://www.figma.com/design/file-id/mock"},
            }
        })

        artifact_text = result["task"]["artifacts"][0]["parts"][0]["text"]
        payload = json.loads(artifact_text)
        assert payload["name"] == "Design"
