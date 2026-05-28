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


@pytest.fixture(autouse=True)
def _default_permission_enforcement_off(monkeypatch):
    monkeypatch.setenv("PERMISSION_ENFORCEMENT", "off")


def _permissions(*allowed_tools: str, scm: str = "read") -> dict:
    return {
        "allowedTools": list(allowed_tools),
        "deniedTools": [],
        "scm": scm,
        "filesystem": "workspace-only",
    }


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

    async def test_ui_design_adapter_propagates_workspace_file_paths(self, monkeypatch, tmp_path):
        stitch = MagicMock()
        stitch.get_screen.return_value = ({"content": [], "text": "{}"}, "ok")
        adapter = UIDesignAgentAdapter(ui_design_definition, _make_services(), stitch_client=stitch)

        monkeypatch.setattr(
            UIDesignAgentAdapter,
            "_persist_workspace_outputs",
            lambda self, cap, result, meta, task_id: {
                **result,
                "local_folder": str(tmp_path / "ui-design" / "stitch"),
                "files": ["ui-design/stitch/code.html", "ui-design/stitch/DESIGN.md"],
                "design_code_path": str(tmp_path / "ui-design" / "stitch" / "code.html"),
                "design_md_path": str(tmp_path / "ui-design" / "stitch" / "DESIGN.md"),
            },
        )

        result = await adapter.handle_message({
            "message": {
                "parts": [{"text": "13629074018280446337"}],
                "metadata": {
                    "requestedCapability": "stitch.screen.fetch",
                    "stitchProjectId": "13629074018280446337",
                    "stitchScreenId": "screen-1",
                    "workspacePath": str(tmp_path),
                },
            }
        })

        artifact = result["task"]["artifacts"][0]
        payload = json.loads(artifact["parts"][0]["text"])
        assert payload["local_folder"].endswith("ui-design/stitch")
        assert artifact["metadata"]["designMdPath"].endswith("ui-design/stitch/DESIGN.md")

    async def test_jira_adapter_denies_missing_permissions_in_strict(self, monkeypatch):
        monkeypatch.setenv("PERMISSION_ENFORCEMENT", "strict")
        provider = MagicMock()
        adapter = JiraAgentAdapter(jira_definition, _make_services(), jira_provider=provider)

        result = await adapter.handle_message({
            "message": {
                "parts": [{"text": "PROJ-123"}],
                "metadata": {"requestedCapability": "jira.ticket.fetch", "ticketKey": "PROJ-123"},
            }
        })

        artifact_text = result["task"]["artifacts"][0]["parts"][0]["text"]
        payload = json.loads(artifact_text)
        assert payload["status"] == "permission_denied"
        provider.fetch_issue.assert_not_called()

    async def test_scm_adapter_denies_missing_tool_permission_in_strict(self, monkeypatch):
        monkeypatch.setenv("PERMISSION_ENFORCEMENT", "strict")
        client = MagicMock()
        adapter = SCMAgentAdapter(scm_definition, _make_services(), scm_client=client)

        result = await adapter.handle_message({
            "message": {
                "parts": [{"text": "PROJ/repo"}],
                "metadata": {
                    "requestedCapability": "scm.pr.diff",
                    "project": "PROJ",
                    "repo": "repo",
                    "prNumber": 42,
                    "permissions": _permissions("clone_repo", scm="read"),
                },
            }
        })

        artifact_text = result["task"]["artifacts"][0]["parts"][0]["text"]
        payload = json.loads(artifact_text)
        assert payload["status"] == "permission_denied"
        client.get_pr_diff.assert_not_called()

    async def test_ui_design_adapter_accepts_permissions_in_strict(self, monkeypatch):
        monkeypatch.setenv("PERMISSION_ENFORCEMENT", "strict")
        figma = MagicMock()
        figma.get_file.return_value = ({"name": "Design", "document": {"children": []}, "lastModified": "now"}, 200)
        adapter = UIDesignAgentAdapter(ui_design_definition, _make_services(), figma_client=figma)

        result = await adapter.handle_message({
            "message": {
                "parts": [{"text": "https://www.figma.com/design/file-id/mock"}],
                "metadata": {
                    "requestedCapability": "figma.file.fetch",
                    "figmaUrl": "https://www.figma.com/design/file-id/mock",
                    "permissions": _permissions("fetch_design", "fetch_figma_page"),
                },
            }
        })

        artifact_text = result["task"]["artifacts"][0]["parts"][0]["text"]
        payload = json.loads(artifact_text)
        assert payload["name"] == "Design"
