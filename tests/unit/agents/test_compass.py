"""Tests for Compass Agent (hybrid heuristic + LLM routing)."""
import asyncio
import json
import pytest
from unittest.mock import MagicMock, patch
from agents.compass.agent import (
    CompassAgent,
    compass_definition,
    _classify_request,
    _extract_office_request,
    _extract_jira_key,
    _parse_classification_payload,
)
from agents.compass.tools import _build_office_execution_contract
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


async def _wait_for_task_state(agent, task_id: str, expected_state: str, attempts: int = 50):
    last = None
    for _ in range(attempts):
        last = await agent.get_task(task_id)
        if last["task"]["status"]["state"] == expected_state:
            return last
        await asyncio.sleep(0.01)
    return last


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

    # ---------------------------------------------------------------
    # Development tasks
    # ---------------------------------------------------------------
    def test_jira_url_with_implement(self):
        assert _classify_request(
            "implement the jira ticket: https://company.atlassian.net/browse/PROJ-2", None
        ) == "development"

    def test_jira_key_with_fix_bug(self):
        assert _classify_request("Fix bug in ABC-123", None) == "development"

    def test_jira_key_with_create_pr(self):
        assert _classify_request("please create pr for PROJ-99", None) == "development"

    def test_jira_url_with_code_review(self):
        assert _classify_request(
            "do a code review for https://jira.company.com/browse/ENG-42", None
        ) == "development"

    def test_jira_url_with_refactor(self):
        assert _classify_request(
            "refactor the auth module https://company.atlassian.net/browse/AUTH-5", None
        ) == "development"

    def test_implement_ticket_phrase(self):
        assert _classify_request(
            "implement the jira ticket https://company.atlassian.net/browse/PROJ-3", None
        ) == "development"

    def test_write_unit_tests(self):
        # Has strong dev action without Jira key — goes to LLM (runtime=None → general),
        # but the heuristic covers "write tests" only with a Jira key. Accepted.
        result = _classify_request("write unit tests for payment service", None)
        assert result in ("development", "general")

    def test_jira_key_alone(self):
        # Has a Jira key but no action keyword — falls through to LLM (runtime=None → general)
        result = _classify_request("CSTL-2", None)
        assert result in ("development", "general")

    def test_lllm_fallback_returns_development(self):
        """When LLM returns 'development', classification follows."""
        mock_runtime = MagicMock()
        mock_runtime.run.return_value = {"raw_response": "development"}
        result = _classify_request("write unit tests for the user service", mock_runtime)
        assert result == "development"

    def test_extract_office_request_parses_absolute_source_path_from_user_text(self):
        request = _extract_office_request(
            'please analyze sales data in "/Users/tony/projects/constellation/tests/data/csv", and show me the report',
            {},
        )

        assert request["capability"] == "analyze"
        assert request["source_paths"] == ["/Users/tony/projects/constellation/tests/data/csv"]

    def test_llm_fallback_returns_development_with_noise(self):
        """LLM may include extra whitespace/punctuation — still parsed correctly."""
        mock_runtime = MagicMock()
        mock_runtime.run.return_value = {"raw_response": "  development\n"}
        result = _classify_request("help me set up docker compose", mock_runtime)
        assert result == "development"

    def test_llm_fallback_returns_office(self):
        mock_runtime = MagicMock()
        mock_runtime.run.return_value = {"raw_response": "office"}
        result = _classify_request("please process my documents", mock_runtime)
        assert result == "office"

    def test_llm_json_payload_is_validated(self):
        assert _parse_classification_payload('{"type":"development","confidence":0.87}') == (
            "development", 0.87
        )

    def test_invalid_llm_json_payload_is_rejected(self):
        assert _parse_classification_payload('{"type":"admin","confidence":0.9}') == ("", 0.0)

    # ---------------------------------------------------------------
    # Office tasks
    # ---------------------------------------------------------------
    def test_office_summarize_pdf(self):
        assert _classify_request("summarize the pdf in my downloads folder", None) == "office"

    def test_office_organize_files(self):
        assert _classify_request("organize files in /home/user/docs", None) == "office"

    def test_office_analyze_spreadsheet(self):
        assert _classify_request("analyze the spreadsheet /data/sales.xlsx", None) == "office"

    # ---------------------------------------------------------------
    # General tasks
    # ---------------------------------------------------------------
    def test_general_question(self):
        assert _classify_request("What is Python?", None) == "general"

    def test_general_greeting(self):
        assert _classify_request("Hello, what can you do?", None) == "general"

    def test_general_explanation(self):
        assert _classify_request("explain how JWT authentication works", None) == "general"

    def test_general_empty(self):
        assert _classify_request("", None) == "general"

    # ---------------------------------------------------------------
    # LLM failure graceful degradation
    # ---------------------------------------------------------------
    def test_llm_failure_falls_back_to_general(self):
        mock_runtime = MagicMock()
        mock_runtime.run.side_effect = RuntimeError("LLM offline")
        result = _classify_request("what should I do today?", mock_runtime)
        assert result == "general"

    def test_llm_unexpected_output_falls_back_to_general(self):
        mock_runtime = MagicMock()
        mock_runtime.run.return_value = {"raw_response": "UNKNOWN_CATEGORY"}
        result = _classify_request("some ambiguous request", mock_runtime)
        assert result == "general"

    # ---------------------------------------------------------------
    # Jira key extraction
    # ---------------------------------------------------------------
    def test_extract_jira_key(self):
        assert _extract_jira_key("implement https://jira.example.com/browse/PROJ-42") == "PROJ-42"
        assert _extract_jira_key("Fix ABC-123 please") == "ABC-123"
        assert _extract_jira_key("no jira here") == ""

    def test_extract_jira_key_from_url(self):
        assert _extract_jira_key(
            "implement the jira ticket: https://company.atlassian.net/browse/PROJ-2"
        ) == "PROJ-2"


class TestCompassAgent:
    def test_builds_office_execution_contract(self):
        contract = _build_office_execution_contract("workspace")

        assert contract["profileName"] == "office"
        assert "read_pdf" in contract["allowedTools"]
        assert contract["workflowRef"] == "config/workflows/office_task.yaml"
        assert contract["checksum"].startswith("sha256:")

    async def test_handles_development_task(self):
        """Development tasks are dispatched directly via registry (not run_agentic)."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)

        message = {
            "parts": [{"text": "implement the jira ticket: https://company.atlassian.net/browse/PROJ-2"}],
            "metadata": {},
        }
        with patch("framework.tools.registry.get_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_reg.execute_sync.return_value = json.dumps({
                "status": "completed",
                "taskId": "tl-001",
                "summary": "Task completed successfully.",
                "prUrl": "https://example.com/pr/1",
                "branch": "feature/PROJ-2",
            })
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_WORKING"
        assert "background" in result["ui_update"]["chat_message"]["text"].lower()
        # Direct dispatch should NOT call run_agentic
        runtime.run_agentic.assert_not_called()

        final_task = await _wait_for_task_state(agent, result["task"]["id"], "TASK_STATE_COMPLETED")
        assert final_task["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        assert "Task completed successfully." in final_task["task"]["artifacts"][0]["parts"][0]["text"]
        mock_reg.execute_sync.assert_called_once()
        call_args = mock_reg.execute_sync.call_args
        assert call_args[0][0] == "dispatch_development_task"
        payload = call_args[0][1]
        assert payload["task_description"] == "implement the jira ticket: https://company.atlassian.net/browse/PROJ-2"
        assert payload["jira_key"] == "PROJ-2"
        assert "orchestratorTaskId" in payload
        assert "workspacePath" in payload

    async def test_handles_office_task(self):
        """Office tasks without output_mode pause for clarification before dispatch."""
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

        assert result["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        runtime.run_agentic.assert_not_called()
        mock_reg.execute_sync.assert_not_called()
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
        runtime.run.return_value = {"raw_response": "general"}
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
            mock_reg.execute_sync.return_value = json.dumps({
                "status": "completed",
                "taskId": "tl-001",
                "summary": "Done.",
            })
            mock_get_reg.return_value = mock_reg
            result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_WORKING"
        # Development task → direct dispatch, not run_agentic
        runtime.run_agentic.assert_not_called()

        final_task = await _wait_for_task_state(agent, result["task"]["id"], "TASK_STATE_COMPLETED")
        assert final_task["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


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
            dispatched["timeout"] = kwargs.get("timeout")
            return {
                "task": {
                    "id": "tl-001",
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
            orchestratorTaskId="compass-001",
            workspacePath="/tmp/workspace/compass-001",
        )

        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert payload["taskId"] == "tl-001"
        assert dispatched["url"] == "http://registry-team-lead:8030"
        assert dispatched["capability"] == "team-lead.task.analyze"
        assert dispatched["metadata"]["jiraKey"] == "PROJ-123"
        assert dispatched["metadata"]["orchestratorTaskId"] == "compass-001"
        assert dispatched["metadata"]["workspacePath"] == "/tmp/workspace/compass-001"
        # Development dispatches use the configurable team-lead timeout,
        # default 5400s (90 min) to accommodate revision rounds.
        assert dispatched["timeout"] == 5400

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
                    "id": "tl-002",
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
        assert payload["taskId"] == "tl-002"
        assert "Jira ticket not accessible" in payload["message"]

    def test_dispatch_development_task_requires_registry_registration(self, monkeypatch):
        from agents.compass.tools import DispatchDevelopmentTask

        class StubRegistryClient:
            def discover(self, capability):
                return ""

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )

        result = DispatchDevelopmentTask().execute_sync(task_description="Implement PROJ-123")
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "No registered Team Lead instance" in payload["message"]

    def test_dispatch_office_task_uses_per_task_launcher_for_workspace_mode(self, monkeypatch, tmp_path):
        from agents.compass.tools import DispatchOfficeTask

        source_dir = tmp_path / "docs"
        source_dir.mkdir()
        source_file = source_dir / "report.txt"
        source_file.write_text("hello", encoding="utf-8")

        launched = {}
        destroyed = {}
        dispatched = {}

        class StubLauncher:
            def resolve_host_path(self, path):
                return f"/host{path}"

            def launch_instance(self, agent_definition, task_id, *, launch_overrides=None):
                launched["agent_id"] = agent_definition.agent_id
                launched["task_id"] = task_id
                launched["overrides"] = launch_overrides
                return {
                    "container_name": "office-task-1",
                    "service_url": "http://office-task-1:8060",
                    "port": 8060,
                }

            def destroy_instance(self, agent_id, container_name):
                destroyed["agent_id"] = agent_id
                destroyed["container_name"] = container_name

        def _dispatch_sync(url, capability, message_parts, metadata, **kwargs):
            dispatched["url"] = url
            dispatched["capability"] = capability
            dispatched["metadata"] = metadata
            return {
                "task": {
                    "id": "office-1",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"text": "Done."}], "metadata": {}}],
                }
            }

        monkeypatch.setattr("agents.compass.tools._should_use_per_task_office_launch", lambda: True)
        monkeypatch.setattr("agents.compass.tools.get_launcher", lambda: StubLauncher())
        monkeypatch.setattr("agents.compass.tools._wait_for_agent_ready", lambda *args, **kwargs: None)
        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        result = DispatchOfficeTask().execute_sync(
            task_description="Summarize the authorized file.",
            source_paths=[str(source_file)],
            output_mode="workspace",
            capability="summarize",
            orchestrator_task_id="compass-123",
        )

        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert launched["agent_id"] == "office"
        assert launched["overrides"]["env"]["OFFICE_SOURCE_ROOT"] == "/app/userdata"
        assert launched["overrides"]["env"]["OFFICE_ALLOW_INPLACE_WRITES"] == "false"
        assert launched["overrides"]["extra_binds"] == [f"/host{source_dir}:/app/userdata/input-0:ro"]
        assert dispatched["url"] == "http://office-task-1:8060"
        assert dispatched["capability"] == "office.document.summarize"
        assert dispatched["metadata"]["source_paths"] == ["/app/userdata/input-0/report.txt"]
        assert "read_pdf" in dispatched["metadata"]["permissions"]["allowedTools"]
        assert "dispatch_office_task" not in dispatched["metadata"]["permissions"]["allowedTools"]
        assert destroyed == {"agent_id": "office", "container_name": "office-task-1"}

    def test_dispatch_office_task_uses_writable_mounts_for_inplace_mode(self, monkeypatch, tmp_path):
        from agents.compass.tools import DispatchOfficeTask

        source_dir = tmp_path / "folder"
        source_dir.mkdir()

        launched = {}

        class StubLauncher:
            def resolve_host_path(self, path):
                return f"/host{path}"

            def launch_instance(self, agent_definition, task_id, *, launch_overrides=None):
                launched["overrides"] = launch_overrides
                return {
                    "container_name": "office-task-2",
                    "service_url": "http://office-task-2:8060",
                    "port": 8060,
                }

            def destroy_instance(self, agent_id, container_name):
                return None

        monkeypatch.setattr("agents.compass.tools._should_use_per_task_office_launch", lambda: True)
        monkeypatch.setattr("agents.compass.tools.get_launcher", lambda: StubLauncher())
        monkeypatch.setattr("agents.compass.tools._wait_for_agent_ready", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "framework.a2a.client.dispatch_sync",
            lambda **kwargs: {
                "task": {
                    "id": "office-2",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"text": "Done."}], "metadata": {}}],
                }
            },
        )

        result = DispatchOfficeTask().execute_sync(
            task_description="Organize the authorized folder.",
            source_paths=[str(source_dir)],
            output_mode="inplace",
            capability="organize",
            orchestrator_task_id="compass-456",
        )

        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert launched["overrides"]["env"]["OFFICE_ALLOW_INPLACE_WRITES"] == "true"
        assert launched["overrides"]["extra_binds"] == [f"/host{source_dir}:/app/userdata/input-0/folder"]
        assert launched["overrides"]["env"]["OFFICE_ALLOWED_BASE_PATHS"] == "/app/userdata/input-0/folder"

    def test_dispatch_office_task_direct_dispatch_attaches_child_permissions(self, monkeypatch, tmp_path):
        from agents.compass.tools import DispatchOfficeTask

        source_file = tmp_path / "report.txt"
        source_file.write_text("hello", encoding="utf-8")
        dispatched = {}

        def _dispatch_sync(url, capability, message_parts, metadata, **kwargs):
            dispatched["url"] = url
            dispatched["capability"] = capability
            dispatched["metadata"] = metadata
            return {
                "task": {
                    "id": "office-direct-1",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"text": "Done."}], "metadata": {}}],
                }
            }

        monkeypatch.setattr("agents.compass.tools._should_use_per_task_office_launch", lambda: False)
        monkeypatch.setattr("agents.compass.tools._resolve_office_url", lambda: "http://office:8060")
        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        result = DispatchOfficeTask().execute_sync(
            task_description="Summarize the authorized file.",
            source_paths=[str(source_file)],
            output_mode="workspace",
            capability="summarize",
            orchestrator_task_id="compass-direct-123",
        )

        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert dispatched["url"] == "http://office:8060"
        assert dispatched["metadata"]["compassTaskId"] == "compass-direct-123"
        assert "permissions" in dispatched["metadata"]
        assert "read_pdf" in dispatched["metadata"]["permissions"]["allowedTools"]
        assert "dispatch_office_task" not in dispatched["metadata"]["permissions"]["allowedTools"]

    def test_dispatch_office_task_translates_host_paths_visible_only_via_workspace_mount(self, monkeypatch, tmp_path):
        from agents.compass.tools import DispatchOfficeTask

        workspace_root = tmp_path / "workspace"
        source_dir = workspace_root / "tests" / "data" / "2026"
        source_dir.mkdir(parents=True)
        (source_dir / "1.txt").write_text("hello", encoding="utf-8")
        requested_host_path = "/Users/test/project/tests/data/2026"

        launched = {}

        class StubLauncher:
            def resolve_container_path(self, path):
                if path == requested_host_path:
                    return str(source_dir)
                return path

            def resolve_host_path(self, path):
                if path == str(source_dir):
                    return "/host-mounted/tests/data/2026"
                return path

            def launch_instance(self, agent_definition, task_id, *, launch_overrides=None):
                launched["overrides"] = launch_overrides
                return {
                    "container_name": "office-task-3",
                    "service_url": "http://office-task-3:8060",
                    "port": 8060,
                }

            def destroy_instance(self, agent_id, container_name):
                return None

        monkeypatch.setattr("agents.compass.tools._should_use_per_task_office_launch", lambda: True)
        monkeypatch.setattr("agents.compass.tools.get_launcher", lambda: StubLauncher())
        monkeypatch.setattr("agents.compass.tools._wait_for_agent_ready", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "framework.a2a.client.dispatch_sync",
            lambda **kwargs: {
                "task": {
                    "id": "office-3",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"text": "Done."}], "metadata": {}}],
                }
            },
        )

        result = DispatchOfficeTask().execute_sync(
            task_description="Organize the authorized folder.",
            source_paths=[requested_host_path],
            output_mode="workspace",
            capability="organize",
            orchestrator_task_id="compass-789",
        )

        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert launched["overrides"]["extra_binds"] == ["/host-mounted/tests/data/2026:/app/userdata/input-0/2026:ro"]
        assert launched["overrides"]["env"]["OFFICE_ALLOWED_BASE_PATHS"] == "/app/userdata/input-0/2026"

    def test_office_dispatch_accepts_registry_definition_for_per_task_launch(self, monkeypatch, tmp_path):
        from agents.compass.agent import _dispatch_office_request

        class StubRegistryClient:
            url = "http://registry:9000"

            def get_capability_definition(self, capability):
                if capability == "office.data.analyze":
                    return {"agent_id": "office", "execution_mode": "per-task"}
                return {}

        class StubRegistry:
            def execute_sync(self, name, arguments):
                assert name == "dispatch_office_task"
                return json.dumps({"status": "completed", "summary": "Office task completed."})

        class StubLog:
            def a2a(self, *args, **kwargs):
                return None

            def info(self, *args, **kwargs):
                return None

            def warn(self, *args, **kwargs):
                return None

            def error(self, *args, **kwargs):
                return None

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("agents.compass.agent._should_use_per_task_office_launch", lambda: True)
        monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

        report_dir = tmp_path / "task-123" / "office"
        report_dir.mkdir(parents=True)
        (report_dir / "task-report.json").write_text(
            json.dumps({"data": {"success": True}}),
            encoding="utf-8",
        )

        result = _dispatch_office_request(
            "task-123",
            "Analyze the authorized data.",
            {"source_paths": ["/workspace/input.csv"], "capability": "analyze", "output_mode": "workspace"},
            StubRegistry(),
            StubLog(),
        )

        assert result["status"] == "completed"

    def test_office_dispatch_fails_closed_when_delivery_report_is_missing(self, monkeypatch, tmp_path):
        from agents.compass.agent import _dispatch_office_request

        class StubRegistryClient:
            url = "http://registry:9000"

            def get_capability_definition(self, capability):
                if capability == "office.data.analyze":
                    return {"agent_id": "office", "execution_mode": "per-task"}
                return {}

        class StubRegistry:
            def execute_sync(self, name, arguments):
                assert name == "dispatch_office_task"
                return json.dumps({"status": "completed", "summary": "Office task completed."})

        class StubLog:
            def a2a(self, *args, **kwargs):
                return None

            def info(self, *args, **kwargs):
                return None

            def warn(self, *args, **kwargs):
                return None

            def error(self, *args, **kwargs):
                return None

        monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("agents.compass.agent._should_use_per_task_office_launch", lambda: True)

        result = _dispatch_office_request(
            "task-missing-report",
            "Analyze the authorized data.",
            {"source_paths": ["/workspace/input.csv"], "capability": "analyze", "output_mode": "workspace"},
            StubRegistry(),
            StubLog(),
        )

        assert result["status"] == "failed"
        assert "task-report.json" in result["message"]
