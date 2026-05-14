"""Tests for Web Dev Agent workflow."""
import json
import pytest
from framework.workflow import START, END
from agents.web_dev.agent import web_dev_workflow, web_dev_definition
from agents.web_dev.tools import SCMCreatePR, register_web_dev_tools
from agents.web_dev.nodes import (
    setup_workspace,
    analyze_task,
    implement_changes,
    run_tests,
    fix_tests,
    create_pr,
    report_result,
    _safe_json,
)


class TestWebDevWorkflowCompile:

    def test_web_dev_workflow_compiles(self):
        compiled = web_dev_workflow.compile()
        assert compiled.name == "web_dev"

    def test_web_dev_workflow_has_all_nodes(self):
        compiled = web_dev_workflow.compile()
        expected_nodes = {
            "prepare_jira", "setup_workspace", "analyze_task", "implement_changes",
            "run_tests", "fix_tests", "self_assess", "fix_gaps",
            "capture_screenshot", "create_pr", "update_jira", "report_result",
        }
        assert expected_nodes == set(compiled.nodes.keys())

    def test_web_dev_definition_fields(self):
        from framework.agent import AgentMode, ExecutionMode
        assert web_dev_definition.agent_id == "web-dev"
        assert web_dev_definition.mode == AgentMode.TASK
        assert web_dev_definition.execution_mode == ExecutionMode.PER_TASK
        assert "react-nextjs" in web_dev_definition.skills

    def test_web_dev_definition_permissions(self):
        assert web_dev_definition.permissions.get("scm") == "read-write"
        assert web_dev_definition.permissions.get("filesystem") == "workspace-only"


class TestSafeJson:

    def test_valid_json_object(self):
        assert _safe_json('{"a": 1}') == {"a": 1}

    def test_valid_json_array(self):
        assert _safe_json('[1, 2]') == [1, 2]

    def test_json_embedded_in_text(self):
        result = _safe_json('Here is the plan: {"branch_name": "feat/x"}')
        assert result == {"branch_name": "feat/x"}

    def test_invalid_returns_fallback(self):
        assert _safe_json("not json", fallback={}) == {}

    def test_none_returns_fallback(self):
        assert _safe_json(None, fallback=[]) == []


class TestWebDevNodes:

    async def test_setup_workspace_no_runtime(self):
        state = {"_task_id": "t-123", "repo_url": "https://github.com/org/repo", "branch_name": "fix/test"}
        result = await setup_workspace(state)
        assert "workspace_path" in result
        assert "repo_path" in result
        assert result["branch_created"] is True
        assert result["branch_name"] == "fix/test"

    async def test_setup_workspace_derives_branch_from_runtime(self):
        """When no branch_name is preset, runtime.run() is called for it."""
        class _MockRuntime:
            def run(self, prompt, **kw):
                return {"raw_response": '{"branch_name": "feature/ABC-1-login", "workspace_notes": "x"}'}

        state = {"_task_id": "t-1", "_runtime": _MockRuntime(), "repo_url": "http://repo", "user_request": "Add login"}
        result = await setup_workspace(state)
        assert result["branch_name"] == "feature/ABC-1-login"

    async def test_analyze_task_uses_analysis(self):
        state = {"analysis": "Implement login feature", "user_request": "Add login"}
        result = await analyze_task(state)
        assert result["implementation_plan"] == "Implement login feature"

    async def test_analyze_task_fallback_to_user_request(self):
        state = {"user_request": "Fix the bug"}
        result = await analyze_task(state)
        assert result["implementation_plan"] == "Fix the bug"

    async def test_implement_changes_no_runtime(self):
        state = {}
        result = await implement_changes(state)
        assert "changes_made" in result
        assert "test mode" in result["implementation_summary"]
        assert result["agentic_success"] is True

    async def test_implement_changes_with_runtime(self):
        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def run_agentic(self, task, **kw):
                return AgenticResult(
                    success=True,
                    summary="Implemented login form in src/login.py",
                    tool_calls=[
                        {"tool": "write_file", "arguments": "src/login.py", "turn": 1},
                    ],
                    backend_used="mock",
                )

        state = {
            "_runtime": _MockRuntime(),
            "user_request": "Add login",
            "implementation_plan": "Create login form",
            "repo_path": "/tmp/repo",
            "branch_name": "feature/login",
        }
        result = await implement_changes(state)
        assert result["agentic_success"] is True
        assert "Implemented" in result["implementation_summary"]

    async def test_run_tests_pass_no_runtime(self):
        state = {"test_cycles": 0}
        result = await run_tests(state)
        assert result["route"] == "pass"
        assert result["test_status"] == "pass"
        assert result["test_cycles"] == 1

    async def test_run_tests_with_passing_runtime(self):
        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def run_agentic(self, task, **kw):
                return AgenticResult(
                    success=True,
                    summary='{"passed": 5, "failed": 0, "output": "All good"}',
                    backend_used="mock",
                )

        state = {"_runtime": _MockRuntime(), "test_cycles": 0}
        result = await run_tests(state)
        assert result["route"] == "pass"

    async def test_run_tests_with_failing_runtime(self):
        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def run_agentic(self, task, **kw):
                return AgenticResult(
                    success=True,
                    summary='{"passed": 2, "failed": 3, "output": "FAILED"}',
                    backend_used="mock",
                )

        state = {"_runtime": _MockRuntime(), "test_cycles": 0}
        result = await run_tests(state)
        assert result["route"] == "fail"
        assert result["test_cycles"] == 1

    async def test_run_tests_max_cycles_proceeds_to_pr(self):
        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def run_agentic(self, task, **kw):
                return AgenticResult(success=True, summary='{"passed": 0, "failed": 1}', backend_used="mock")

        state = {"_runtime": _MockRuntime(), "test_cycles": 4}  # already at max-1
        result = await run_tests(state)
        assert result["route"] == "pass"  # proceed despite failure

    async def test_fix_tests_no_runtime(self):
        state = {}
        result = await fix_tests(state)
        assert result["fix_attempted"] is True

    async def test_fix_tests_with_runtime(self):
        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def run_agentic(self, task, **kw):
                return AgenticResult(success=True, summary="Fixed null check in login.py", backend_used="mock")

        state = {
            "_runtime": _MockRuntime(),
            "test_output": "AssertionError: None is not True",
            "repo_path": "/tmp/repo",
            "changes_made": ["src/login.py"],
        }
        result = await fix_tests(state)
        assert result["fix_attempted"] is True
        assert "Fixed" in result["fix_summary"]

    async def test_create_pr_no_runtime(self):
        state = {}
        result = await create_pr(state)
        assert "pr_url" in result

    async def test_create_pr_with_runtime(self):
        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def run(self, prompt, **kw):
                return {"raw_response": '{"title": "Add login page", "description": "## Summary\\nAdded login."}'}

            def run_agentic(self, task, **kw):
                return AgenticResult(
                    success=True,
                    summary='{"pr_url": "https://github.com/org/repo/pull/42", "pr_number": 42, "commit_hash": "abc123"}',
                    backend_used="mock",
                )

        state = {
            "_runtime": _MockRuntime(),
            "user_request": "Add login",
            "branch_name": "feature/login",
            "implementation_summary": "Added login form",
            "changes_made": ["src/login.py"],
            "jira_context": {"key": "ABC-123"},
        }
        result = await create_pr(state)
        assert result["pr_url"] == "https://github.com/org/repo/pull/42"
        assert result["pr_title"] == "Add login page"

    async def test_report_result(self):
        state = {
            "pr_url": "https://pr/1",
            "pr_title": "Add login",
            "changes_made": ["a.py", "b.py"],
            "test_status": "pass",
        }
        result = await report_result(state)
        assert result["success"] is True
        assert result["state"] == "TASK_STATE_COMPLETED"
        assert "2 file(s)" in result["summary"]


class TestWebDevWorkflowExecution:

    async def test_happy_path_no_runtime(self):
        compiled = web_dev_workflow.compile()
        state = {
            "_task_id": "test-123",
            "repo_url": "https://github.com/test",
            "branch_name": "fix/ABC-123",
            "analysis": "Fix the login button",
            "user_request": "Fix login",
            "test_cycles": 0,
        }
        result = await compiled.invoke(state)
        assert result["success"] is True
        assert result["state"] == "TASK_STATE_COMPLETED"

    async def test_workflow_state_keys_populated(self):
        compiled = web_dev_workflow.compile()
        state = {"_task_id": "t-1", "user_request": "Add feature"}
        result = await compiled.invoke(state)
        assert "workspace_path" in result
        assert "implementation_plan" in result
        assert "test_results" in result
        assert "success" in result


class TestWebDevBoundaryTools:

    def test_register_web_dev_tools_includes_scm_tools(self):
        from framework.tools.registry import get_registry

        register_web_dev_tools()
        registry = get_registry()
        assert registry.get("scm_push") is not None
        assert registry.get("scm_create_pr") is not None

    def test_scm_create_pr_derives_bitbucket_coordinates(self, monkeypatch):
        dispatched = {}

        def _dispatch_sync(url, capability, message_parts, metadata, **kwargs):
            dispatched["capability"] = capability
            dispatched["metadata"] = metadata
            return {
                "task": {
                    "artifacts": [
                        {"parts": [{"text": json.dumps({"prUrl": "https://bitbucket/pr/42", "status": 201})}]}
                    ]
                }
            }

        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        result = SCMCreatePR().execute_sync(
            repo_url="https://bitbucket.example.com/projects/PROJ/repos/web-ui-test/browse",
            source_branch="feature/proj-123",
            target_branch="main",
            title="PROJ-123: add login page",
            description="PR body",
        )

        payload = json.loads(result.output)
        assert payload["prUrl"] == "https://bitbucket/pr/42"
        assert dispatched["capability"] == "scm.pr.create"
        assert dispatched["metadata"]["project"] == "PROJ"
        assert dispatched["metadata"]["repo"] == "web-ui-test"

