#!/usr/bin/env python3
"""Tests for the common control tools added in Phase 3."""

from __future__ import annotations

import json
import os
import unittest
from urllib.error import URLError
from unittest.mock import patch, MagicMock

# Import at module level so register_tool() runs once and stays registered.
import common.tools.control_tools as _ctrl
import common.tools.scm_tools as _scm          # noqa: F401
import common.tools.dev_agent_tools             # noqa: F401
import common.tools.team_lead_tools             # noqa: F401

from common.tools.registry import get_tool, list_tools


def _tool_names() -> set:
    return {t.schema.name for t in list_tools()}


def _reset_callbacks():
    _ctrl.configure_control_tools(
        task_context={},
        complete_fn=None,
        fail_fn=None,
        input_required_fn=None,
    )


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

class ControlToolRegistrationTests(unittest.TestCase):
    def test_all_control_tools_registered(self):
        names = _tool_names()
        expected = {
            "dispatch_agent_task", "wait_for_agent_task", "ack_agent_task",
            "complete_current_task", "fail_current_task",
            "get_task_context", "get_agent_runtime_status",
            "request_user_input", "request_agent_clarification",
        }
        self.assertTrue(expected.issubset(names), f"Missing: {expected - names}")

    def test_dev_agent_barrel_includes_control_tools(self):
        names = _tool_names()
        self.assertIn("dispatch_agent_task", names)
        self.assertIn("complete_current_task", names)
        self.assertIn("fail_current_task", names)

    def test_team_lead_barrel_includes_control_tools(self):
        names = _tool_names()
        self.assertIn("dispatch_agent_task", names)
        self.assertIn("ack_agent_task", names)
        self.assertIn("request_user_input", names)


# ---------------------------------------------------------------------------
# dispatch_agent_task
# ---------------------------------------------------------------------------

class DispatchAgentTaskTests(unittest.TestCase):
    def setUp(self):
        self.tool = get_tool("dispatch_agent_task")

    def tearDown(self):
        _reset_callbacks()

    def test_discover_capability_url_accepts_registry_list_shape(self):
        payload = [
            {
                "agent_id": "office-agent",
                "instances": [{"service_url": "http://office-agent:8060"}],
            }
        ]

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(payload).encode()

        with patch("common.tools.control_tools.urlopen", return_value=_R()):
            url = _ctrl._discover_capability_url("office.data.analyze")

        self.assertEqual(url, "http://office-agent:8060")

    def test_discover_capability_url_falls_back_to_card_url(self):
        payload = [
            {
                "agent_id": "office-agent",
                "card_url": "http://office-agent:8060/.well-known/agent-card.json",
                "instances": [],
            }
        ]

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(payload).encode()

        with patch("common.tools.control_tools.urlopen", return_value=_R()):
            url = _ctrl._discover_capability_url("office.data.analyze")

        self.assertEqual(url, "http://office-agent:8060")

    def test_discover_live_instance_url_skips_unreachable_stale_instance(self):
        payload = [
            {
                "agent_id": "team-lead-agent",
                "instances": [
                    {
                        "service_url": "http://team-lead-agent-task-old:8030",
                        "last_heartbeat_at": 100,
                        "idle_since": 100,
                    },
                    {
                        "service_url": "http://team-lead-agent-task-new:8030",
                        "last_heartbeat_at": 200,
                        "idle_since": 200,
                    },
                ],
            }
        ]

        class _QueryResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(payload).encode()

        class _HealthResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"status": "ok"}'

        def fake_urlopen(req, timeout=5):
            url = req.full_url
            if url.endswith("/query?capability=team-lead.task.analyze"):
                return _QueryResponse()
            if url == "http://team-lead-agent-task-new:8030/health":
                return _HealthResponse()
            if url == "http://team-lead-agent-task-old:8030/health":
                raise URLError("Name or service not known")
            raise AssertionError(f"Unexpected URL: {url}")

        with patch("common.tools.control_tools.urlopen", side_effect=fake_urlopen):
            url = _ctrl._discover_live_instance_url("team-lead.task.analyze")

        self.assertEqual(url, "http://team-lead-agent-task-new:8030")

    def test_discover_live_instance_url_returns_none_when_all_instances_unreachable(self):
        payload = [
            {
                "agent_id": "team-lead-agent",
                "instances": [
                    {
                        "service_url": "http://team-lead-agent-task-old:8030",
                        "last_heartbeat_at": 100,
                        "idle_since": 100,
                    }
                ],
            }
        ]

        class _QueryResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(payload).encode()

        def fake_urlopen(req, timeout=5):
            url = req.full_url
            if url.endswith("/query?capability=team-lead.task.analyze"):
                return _QueryResponse()
            if url == "http://team-lead-agent-task-old:8030/health":
                raise URLError("Name or service not known")
            raise AssertionError(f"Unexpected URL: {url}")

        with patch("common.tools.control_tools.urlopen", side_effect=fake_urlopen):
            url = _ctrl._discover_live_instance_url("team-lead.task.analyze")

        self.assertIsNone(url)

    def test_missing_capability_returns_error(self):
        result = self.tool.execute({"task_text": "do something"})
        self.assertTrue(result["isError"])
        self.assertIn("capability", result["content"][0]["text"])

    def test_missing_task_text_returns_error(self):
        result = self.tool.execute({"capability": "android.task.execute"})
        self.assertTrue(result["isError"])
        self.assertIn("task_text", result["content"][0]["text"])

    def test_registry_unavailable_returns_error(self):
        with patch("common.tools.control_tools._discover_capability_url", return_value=None):
            result = self.tool.execute({
                "capability": "android.task.execute",
                "task_text": "Implement login screen",
            })
        self.assertTrue(result["isError"])
        self.assertIn("No agent available", result["content"][0]["text"])

    def test_dispatch_succeeds(self):
        mock_response = {"task": {"id": "task-abc", "status": {"state": "TASK_STATE_WORKING"}}}

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(mock_response).encode()

        with patch("common.tools.control_tools._discover_capability_url", return_value="http://agent:8000"), \
             patch("common.tools.control_tools.urlopen", return_value=_R()):
            result = self.tool.execute({
                "capability": "android.task.execute",
                "task_text": "Implement login screen",
            })

        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["taskId"], "task-abc")
        self.assertEqual(data["agentUrl"], "http://agent:8000")

    def test_dispatch_passes_metadata(self):
        mock_response = {"task": {"id": "task-xyz", "status": {"state": "submitted"}}}
        captured = {}

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(mock_response).encode()

        def fake_urlopen(req, timeout=15):
            captured["body"] = json.loads(req.data.decode())
            return _R()

        with patch("common.tools.control_tools._discover_capability_url", return_value="http://agent:8000"), \
             patch("common.tools.control_tools.urlopen", side_effect=fake_urlopen):
            self.tool.execute({
                "capability": "android.task.execute",
                "task_text": "Build login feature",
                "metadata": {"jiraContext": {"ticketKey": "PROJ-1"}},
            })

        msg = captured["body"]["message"]
        self.assertEqual(msg["metadata"]["requestedCapability"], "android.task.execute")
        self.assertEqual(msg["metadata"]["jiraContext"]["ticketKey"], "PROJ-1")

    def test_dispatch_auto_launches_per_task_agent_without_live_instance(self):
        mock_response = {"task": {"id": "task-tl", "status": {"state": "TASK_STATE_WORKING"}}}

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(mock_response).encode()

        with patch("common.tools.control_tools._is_per_task_agent", return_value=True), \
             patch("common.tools.control_tools._discover_live_instance_url", return_value=None), \
             patch.object(self.tool, "_auto_launch", return_value=("http://team-lead-task-123:8030", "")) as auto_launch, \
             patch("common.tools.control_tools.urlopen", return_value=_R()):
            result = self.tool.execute({
                "capability": "team-lead.task.analyze",
                "task_text": "implement jira ticket: https://example.atlassian.net/browse/PROJ-1",
            })

        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["agentUrl"], "http://team-lead-task-123:8030")
        auto_launch.assert_called_once_with("team-lead.task.analyze", [])

    def test_office_dispatch_infers_target_paths_from_task_context(self):
        mock_response = {"task": {"id": "task-office", "status": {"state": "submitted"}}}
        captured = {}

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(mock_response).encode()

        def fake_urlopen(req, timeout=15):
            captured["body"] = json.loads(req.data.decode())
            return _R()

        _ctrl.configure_control_tools(
            task_context={
                "taskId": "task-parent",
                "agentId": "compass-agent",
                "workspacePath": "/app/artifacts/workspaces/task-parent",
                "permissions": None,
                "userText": "Analyze /Users/example/tests/data/csv/sales_data.csv and find the top rep.",
            },
            complete_fn=None,
            fail_fn=None,
            input_required_fn=None,
        )

        with patch("common.tools.control_tools._discover_capability_url", return_value="http://office-agent:8060"), \
             patch("common.tools.control_tools.urlopen", side_effect=fake_urlopen):
            self.tool.execute({
                "capability": "office.data.analyze",
                "task_text": "Analyze /Users/example/tests/data/csv/sales_data.csv and find the top rep.",
                "extra_binds": [
                    "/Users/example/tests/data/csv:/app/userdata:ro",
                    "/app/artifacts/workspaces/task-parent:/app/workspace:rw",
                ],
            })

        msg = captured["body"]["message"]
        self.assertEqual(msg["metadata"]["requestedCapability"], "office.data.analyze")
        self.assertEqual(msg["metadata"]["officeTargetPaths"], ["/app/userdata/sales_data.csv"])
        self.assertEqual(msg["metadata"]["officeHostTargetPaths"], ["/Users/example/tests/data/csv/sales_data.csv"])
        self.assertEqual(msg["metadata"]["sharedWorkspacePath"], "/app/artifacts/workspaces/task-parent")
        self.assertEqual(msg["metadata"]["permissions"]["taskType"], "office")
        self.assertIn("/app/userdata/sales_data.csv", msg["parts"][0]["text"])
        self.assertNotIn("/Users/example/tests/data/csv/sales_data.csv", msg["parts"][0]["text"])

    def test_dispatch_injects_orchestrator_callback_url_when_advertised_url_is_set(self):
        """When advertisedUrl is in task_context, orchestratorCallbackUrl should be injected."""
        mock_response = {"task": {"id": "task-office-cb", "status": {"state": "submitted"}}}
        captured = {}

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(mock_response).encode()

        def fake_urlopen(req, timeout=15):
            captured["body"] = json.loads(req.data.decode())
            return _R()

        _ctrl.configure_control_tools(
            task_context={
                "taskId": "task-cb-parent",
                "agentId": "compass-agent",
                "workspacePath": "/app/artifacts/workspaces/task-cb-parent",
                "advertisedUrl": "http://compass:8080",
                "permissions": None,
                "userText": "Analyze /app/userdata/data.csv",
            },
            complete_fn=None,
            fail_fn=None,
            input_required_fn=None,
        )

        with patch("common.tools.control_tools._discover_capability_url", return_value="http://office-agent:8060"), \
             patch("common.tools.control_tools.urlopen", side_effect=fake_urlopen):
            self.tool.execute({
                "capability": "office.data.analyze",
                "task_text": "Analyze /app/userdata/data.csv",
                "metadata": {"officeTargetPaths": ["/app/userdata/data.csv"]},
            })

        msg = captured["body"]["message"]
        self.assertEqual(
            msg["metadata"]["orchestratorCallbackUrl"],
            "http://compass:8080/tasks/task-cb-parent/callbacks",
        )
        self.assertEqual(msg["metadata"]["orchestratorTaskId"], "task-cb-parent")

    def test_dispatch_does_not_overwrite_existing_orchestrator_callback_url(self):
        """Caller-provided orchestratorCallbackUrl must not be overwritten."""
        mock_response = {"task": {"id": "task-office-existing", "status": {"state": "submitted"}}}
        captured = {}

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(mock_response).encode()

        def fake_urlopen(req, timeout=15):
            captured["body"] = json.loads(req.data.decode())
            return _R()

        _ctrl.configure_control_tools(
            task_context={
                "taskId": "task-existing-cb",
                "agentId": "compass-agent",
                "workspacePath": "/app/artifacts/workspaces/task-existing-cb",
                "advertisedUrl": "http://compass:8080",
                "permissions": None,
                "userText": "Analyze data",
            },
            complete_fn=None,
            fail_fn=None,
            input_required_fn=None,
        )

        with patch("common.tools.control_tools._discover_capability_url", return_value="http://office-agent:8060"), \
             patch("common.tools.control_tools.urlopen", side_effect=fake_urlopen):
            self.tool.execute({
                "capability": "office.data.analyze",
                "task_text": "Analyze /app/userdata/data.csv",
                "metadata": {
                    "officeTargetPaths": ["/app/userdata/data.csv"],
                    "orchestratorCallbackUrl": "http://custom-compass:9090/tasks/custom-task/callbacks",
                },
            })

        msg = captured["body"]["message"]
        self.assertEqual(
            msg["metadata"]["orchestratorCallbackUrl"],
            "http://custom-compass:9090/tasks/custom-task/callbacks",
        )


# ---------------------------------------------------------------------------
# wait_for_agent_task
# ---------------------------------------------------------------------------

class WaitForAgentTaskTests(unittest.TestCase):
    def setUp(self):
        self.tool = get_tool("wait_for_agent_task")

    def test_missing_agent_url_returns_error(self):
        result = self.tool.execute({"task_id": "task-123"})
        self.assertTrue(result["isError"])

    def test_missing_task_id_returns_error(self):
        result = self.tool.execute({"agent_url": "http://agent:8000"})
        self.assertTrue(result["isError"])

    def test_completed_task_returned(self):
        completed = {"id": "task-123", "status": {"state": "TASK_STATE_COMPLETED"}}

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"task": completed}).encode()

        with patch("common.tools.control_tools.urlopen", return_value=_R()):
            result = self.tool.execute({
                "agent_url": "http://agent:8000",
                "task_id": "task-123",
                "timeout": 30,
            })

        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        state = (data.get("status") or {}).get("state") or data.get("state")
        self.assertEqual(state, "TASK_STATE_COMPLETED")

    def test_timeout_returns_error(self):
        working = {"id": "task-123", "status": {"state": "TASK_STATE_WORKING"}}

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"task": working}).encode()

        call_count = [0]

        def fake_time():
            call_count[0] += 1
            # First call returns 0 (start), subsequent calls return past timeout
            return 0.0 if call_count[0] <= 1 else 10000.0

        with patch("common.tools.control_tools.urlopen", return_value=_R()), \
             patch("common.tools.control_tools.time") as mock_time:
            mock_time.time.side_effect = fake_time
            mock_time.sleep = MagicMock()
            result = self.tool.execute({
                "agent_url": "http://agent:8000",
                "task_id": "task-123",
                "timeout": 1,
            })

        self.assertTrue(result["isError"])


# ---------------------------------------------------------------------------
# ack_agent_task
# ---------------------------------------------------------------------------

class AckAgentTaskTests(unittest.TestCase):
    def setUp(self):
        self.tool = get_tool("ack_agent_task")

    def test_missing_args_return_error(self):
        self.assertTrue(self.tool.execute({}).get("isError"))
        self.assertTrue(self.tool.execute({"task_id": "t1"}).get("isError"))
        self.assertTrue(self.tool.execute({"agent_url": "http://a"}).get("isError"))

    def test_ack_succeeds(self):
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'

        with patch("common.tools.control_tools.urlopen", return_value=_R()):
            result = self.tool.execute({
                "agent_url": "http://agent:8000",
                "task_id": "task-999",
            })
        self.assertFalse(result["isError"])


# ---------------------------------------------------------------------------
# complete_current_task
# ---------------------------------------------------------------------------

class CompleteCurrentTaskTests(unittest.TestCase):
    def setUp(self):
        _reset_callbacks()
        self.tool = get_tool("complete_current_task")

    def tearDown(self):
        _reset_callbacks()

    def test_missing_result_text_returns_error(self):
        result = self.tool.execute({})
        self.assertTrue(result["isError"])

    def test_signal_returned_when_no_callback(self):
        result = self.tool.execute({"result_text": "All done!"})
        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["__signal__"], "complete_task")

    def test_callback_invoked(self):
        called = {}
        _ctrl.configure_control_tools(complete_fn=lambda r, a: called.update({"r": r, "a": a}))
        result = self.tool.execute({"result_text": "Task complete", "artifacts": [{"name": "pr"}]})
        self.assertFalse(result["isError"])
        self.assertEqual(called["r"], "Task complete")
        self.assertEqual(len(called["a"]), 1)


# ---------------------------------------------------------------------------
# fail_current_task
# ---------------------------------------------------------------------------

class FailCurrentTaskTests(unittest.TestCase):
    def setUp(self):
        _reset_callbacks()
        self.tool = get_tool("fail_current_task")

    def tearDown(self):
        _reset_callbacks()

    def test_missing_error_message_returns_error(self):
        result = self.tool.execute({})
        self.assertTrue(result["isError"])

    def test_signal_returned_when_no_callback(self):
        result = self.tool.execute({
            "error_message": "Build failed",
            "error_type": "tool_error",
            "retriable": True,
        })
        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["__signal__"], "fail_task")
        self.assertTrue(data["retriable"])

    def test_callback_invoked(self):
        called = {}
        _ctrl.configure_control_tools(fail_fn=lambda e: called.update({"e": e}))
        result = self.tool.execute({"error_message": "Out of retries"})
        self.assertFalse(result["isError"])
        self.assertEqual(called["e"], "Out of retries")


# ---------------------------------------------------------------------------
# get_task_context
# ---------------------------------------------------------------------------

class GetTaskContextTests(unittest.TestCase):
    def setUp(self):
        _reset_callbacks()
        self.tool = get_tool("get_task_context")

    def tearDown(self):
        _reset_callbacks()

    def test_configured_context_returned(self):
        _ctrl.configure_control_tools(task_context={"taskId": "ctx-001", "workspacePath": "/ws"})
        result = self.tool.execute({})
        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["taskId"], "ctx-001")

    def test_env_fallback_when_context_empty(self):
        _ctrl.configure_control_tools(task_context={})
        with patch.dict(os.environ, {"TASK_ID": "env-123", "SHARED_WORKSPACE_PATH": "/ws/env-123"}):
            result = self.tool.execute({})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["taskId"], "env-123")
        self.assertEqual(data["workspacePath"], "/ws/env-123")


# ---------------------------------------------------------------------------
# get_agent_runtime_status
# ---------------------------------------------------------------------------

class GetAgentRuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        self.tool = get_tool("get_agent_runtime_status")

    def test_returns_effective_backend(self):
        with patch.dict(os.environ, {"AGENT_RUNTIME": "connect-agent"}):
            result = self.tool.execute({})
        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["effectiveBackend"], "connect-agent")

    def test_reports_agent_model(self):
        with patch.dict(os.environ, {"AGENT_RUNTIME": "connect-agent", "AGENT_MODEL": "gpt-5-model"}):
            result = self.tool.execute({})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data.get("model"), "gpt-5-model")


# ---------------------------------------------------------------------------
# request_user_input
# ---------------------------------------------------------------------------

class RequestUserInputTests(unittest.TestCase):
    def setUp(self):
        _reset_callbacks()
        self.tool = get_tool("request_user_input")

    def tearDown(self):
        _reset_callbacks()

    def test_missing_question_returns_error(self):
        result = self.tool.execute({})
        self.assertTrue(result["isError"])

    def test_signal_when_no_callback(self):
        result = self.tool.execute({"question": "Which environment?", "context": "Staging or prod."})
        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["__signal__"], "input_required")

    def test_callback_invoked(self):
        called = {}
        _ctrl.configure_control_tools(input_required_fn=lambda q, c: called.update({"q": q, "c": c}))
        result = self.tool.execute({"question": "Which repo?", "context": "Multiple repos"})
        self.assertFalse(result["isError"])
        self.assertEqual(called["q"], "Which repo?")


# ---------------------------------------------------------------------------
# request_agent_clarification
# ---------------------------------------------------------------------------

class RequestAgentClarificationTests(unittest.TestCase):
    def setUp(self):
        self.tool = get_tool("request_agent_clarification")

    def test_missing_question_returns_error(self):
        result = self.tool.execute({})
        self.assertTrue(result["isError"])

    def test_unavailable_agent_returns_error(self):
        with patch("common.tools.control_tools._discover_capability_url", return_value=None):
            result = self.tool.execute({"question": "What env?"})
        self.assertTrue(result["isError"])
        self.assertIn("No agent available", result["content"][0]["text"])

    def test_succeeds_with_known_agent(self):
        mock_response = {"task": {"id": "clarify-1", "status": {"state": "submitted"}}}

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(mock_response).encode()

        with patch("common.tools.control_tools._discover_capability_url", return_value="http://compass:8080"), \
             patch("common.tools.control_tools.urlopen", return_value=_R()):
            result = self.tool.execute({
                "question": "What prefix?",
                "target_capability": "team-lead.task.analyze",
            })

        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertTrue(data["sent"])


# ---------------------------------------------------------------------------
# SCM read-only tools
# ---------------------------------------------------------------------------

class ScmReadOnlyToolRegistrationTests(unittest.TestCase):
    def test_all_scm_read_only_tools_registered(self):
        names = _tool_names()
        expected = {
            "scm_read_file", "scm_list_dir", "scm_search_code",
            "scm_compare_refs", "scm_get_default_branch", "scm_get_branch_rules",
            "scm_get_pr_details", "scm_get_pr_diff",
            "scm_list_branches", "scm_clone_repo", "scm_repo_inspect",
        }
        self.assertTrue(expected.issubset(names), f"Missing SCM tools: {expected - names}")

    def test_scm_read_file_schema(self):
        tool = get_tool("scm_read_file")
        props = tool.schema.input_schema["properties"]
        self.assertIn("owner", props)
        self.assertIn("repo", props)
        self.assertIn("path", props)

    def test_scm_compare_refs_required_fields(self):
        tool = get_tool("scm_compare_refs")
        for f in ("owner", "repo", "base", "head"):
            self.assertIn(f, tool.schema.input_schema["required"])

    def test_scm_read_file_unavailable_returns_error(self):
        tool = get_tool("scm_read_file")
        with patch("common.tools.scm_tools._discover_scm_url", return_value=None):
            result = tool.execute({"owner": "org", "repo": "myapp", "path": "README.md"})
        self.assertTrue(result["isError"])
        self.assertIn("SCM Agent is not available", result["content"][0]["text"])

    def test_scm_get_default_branch_calls_rest(self):
        tool = get_tool("scm_get_default_branch")
        mock_data = {"defaultBranch": "main", "protectedBranches": ["main"]}

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(mock_data).encode()

        with patch("common.tools.scm_tools._discover_scm_url", return_value="http://scm:8020"), \
             patch("common.tools.scm_tools.urlopen", return_value=_R()):
            result = tool.execute({"owner": "org", "repo": "myapp"})

        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["defaultBranch"], "main")

    def test_scm_search_code_unavailable(self):
        tool = get_tool("scm_search_code")
        with patch("common.tools.scm_tools._discover_scm_url", return_value=None):
            result = tool.execute({"owner": "org", "repo": "myapp", "query": "def login"})
        self.assertTrue(result["isError"])

    def test_scm_clone_repo_unavailable(self):
        tool = get_tool("scm_clone_repo")
        with patch("common.tools.scm_tools._discover_scm_url", return_value=None):
            result = tool.execute({"repo": "https://github.com/org/repo"})
        self.assertTrue(result["isError"])


# ---------------------------------------------------------------------------
# Runtime model resolution (Phase 1)
# ---------------------------------------------------------------------------

class RuntimeModelResolutionTests(unittest.TestCase):
    """Phase 1: summarize_runtime_configuration must use only AGENT_MODEL."""

    def test_copilot_cli_uses_agent_model(self):
        with patch.dict(os.environ, {
            "AGENT_RUNTIME": "copilot-cli",
            "AGENT_MODEL": "unified-model",
            "COPILOT_MODEL": "copilot-specific",
            "OPENAI_MODEL": "openai-specific",
        }):
            from common.runtime.adapter import summarize_runtime_configuration
            summary = summarize_runtime_configuration()
        self.assertEqual(summary.get("model"), "unified-model")

    def test_claude_code_uses_agent_model(self):
        with patch.dict(os.environ, {
            "AGENT_RUNTIME": "claude-code",
            "AGENT_MODEL": "claude-unified",
            "CLAUDE_CODE_MODEL": "claude-specific",
        }):
            from common.runtime.adapter import summarize_runtime_configuration
            summary = summarize_runtime_configuration()
        self.assertEqual(summary.get("model"), "claude-unified")

    def test_connect_agent_uses_agent_model(self):
        with patch.dict(os.environ, {
            "AGENT_RUNTIME": "connect-agent",
            "AGENT_MODEL": "connect-unified",
            "OPENAI_MODEL": "openai-specific",
        }):
            from common.runtime.adapter import summarize_runtime_configuration
            summary = summarize_runtime_configuration()
        self.assertEqual(summary.get("model"), "connect-unified")

    def test_fallback_to_gpt5_mini_when_no_agent_model(self):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("AGENT_MODEL", "OPENAI_MODEL", "COPILOT_MODEL", "CLAUDE_CODE_MODEL")}
        clean["AGENT_RUNTIME"] = "connect-agent"
        with patch.dict(os.environ, clean, clear=True):
            from common.runtime.adapter import summarize_runtime_configuration
            summary = summarize_runtime_configuration()
        self.assertEqual(summary.get("model"), "gpt-5-mini")


# ---------------------------------------------------------------------------
# configure_control_tools integration
# ---------------------------------------------------------------------------

class ConfigureControlToolsTests(unittest.TestCase):
    def tearDown(self):
        _reset_callbacks()

    def test_all_callbacks_wired(self):
        called = {}
        _ctrl.configure_control_tools(
            task_context={"taskId": "t-001"},
            complete_fn=lambda r, a: called.update({"complete": r}),
            fail_fn=lambda e: called.update({"fail": e}),
            input_required_fn=lambda q, c: called.update({"input": q}),
        )

        get_tool("complete_current_task").execute({"result_text": "done"})
        get_tool("fail_current_task").execute({"error_message": "error"})
        get_tool("request_user_input").execute({"question": "which env?"})

        self.assertEqual(called.get("complete"), "done")
        self.assertEqual(called.get("fail"), "error")
        self.assertEqual(called.get("input"), "which env?")

    def test_task_context_via_get_task_context(self):
        _ctrl.configure_control_tools(task_context={
            "taskId": "configured-task",
            "permissions": {"allowed": []},
        })
        result = get_tool("get_task_context").execute({})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["taskId"], "configured-task")
        self.assertIn("permissions", data)


if __name__ == "__main__":
    unittest.main()
