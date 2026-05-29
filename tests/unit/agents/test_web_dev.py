"""Tests for Web Dev Agent workflow."""
import json
import os
import pytest
from unittest.mock import MagicMock
from framework.agent import AgentServices
from framework.task_store import InMemoryTaskStore
from framework.workflow import START, END
from agents.web_dev.agent import WebDevAgent, web_dev_workflow, web_dev_definition
from agents.web_dev.tools import SCMCreatePR, SCMUploadPRImage, register_web_dev_tools
from agents.web_dev.nodes import (
    setup_workspace,
    analyze_task,
    implement_changes,
    run_tests,
    fix_tests,
    self_assess,
    capture_screenshot,
    create_pr,
    report_result,
    _call_boundary_tool,
    _safe_json,
    _detect_fragile_icon_font_usage,
    _rendered_page_has_content,
)


def _agent_services(runtime=None):
    return AgentServices(
        session_service=MagicMock(),
        event_store=MagicMock(),
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=runtime or MagicMock(),
        registry_client=None,
        task_store=InMemoryTaskStore(),
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
            "pause_for_user_input",
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

    def test_web_dev_definition_tools_include_runtime_boundary_calls(self):
        assert "jira_update" in web_dev_definition.tools
        assert "scm_list_branches" in web_dev_definition.tools


class TestWebDevExecutionContract:

    async def test_handle_message_fails_closed_without_execution_contract(self):
        agent = WebDevAgent(definition=web_dev_definition, services=_agent_services())

        result = await agent.handle_message({"message": {"parts": [{"text": "Implement task"}], "metadata": {}}})

        assert result["task"]["status"]["state"] == "TASK_STATE_FAILED"
        assert "Missing executionContract" in result["task"]["status"]["message"]["parts"][0]["text"]

    async def test_handle_message_rejects_contract_broader_than_local_profile(self):
        from framework.execution_contract import build_execution_contract

        agent = WebDevAgent(definition=web_dev_definition, services=_agent_services())
        broad_contract = build_execution_contract(
            profile={"agent_id": "web-dev", "allowed_tools": ["read_file", "dispatch_web_dev"]},
            workflow_ref="config/workflows/development_task.yaml",
            rule_refs=[],
            workspace_root="/tmp/workspace",
        )

        result = await agent.handle_message({
            "message": {
                "parts": [{"text": "Implement feature"}],
                "metadata": {"executionContract": broad_contract.to_dict()},
            }
        })

        assert result["task"]["status"]["state"] == "TASK_STATE_FAILED"
        assert "exceed local profile" in result["task"]["status"]["message"]["parts"][0]["text"]


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


class TestScreenshotRenderChecks:

    def test_rendered_page_has_content_accepts_visible_dom(self):
        assert _rendered_page_has_content(
            {
                "rootChildren": 1,
                "bodyChildren": 2,
                "visibleTextChars": 120,
                "bodyWidth": 1280,
                "bodyHeight": 900,
            }
        ) is True

    def test_rendered_page_has_content_rejects_blank_page(self):
        assert _rendered_page_has_content(
            {
                "rootChildren": 0,
                "bodyChildren": 1,
                "visibleTextChars": 0,
                "bodyWidth": 1280,
                "bodyHeight": 900,
            }
        ) is False

    def test_detect_fragile_icon_font_usage_flags_material_ligatures(self, tmp_path):
        repo_path = tmp_path / "repo"
        src_path = repo_path / "src"
        src_path.mkdir(parents=True)
        (src_path / "Hero.jsx").write_text(
            "<span className=\"material-symbols-outlined\">arrow_forward</span>\n",
            encoding="utf-8",
        )

        findings = _detect_fragile_icon_font_usage(str(repo_path))

        assert findings["issues"]
        assert findings["uses_material_icon_class"] is True
        assert findings["uses_remote_material_font"] is False
        assert "arrow_forward" in findings["icon_tokens"]

    def test_detect_fragile_icon_font_usage_ignores_plain_words(self, tmp_path):
        repo_path = tmp_path / "repo"
        src_path = repo_path / "src"
        src_path.mkdir(parents=True)
        (src_path / "LandingPage.jsx").write_text(
            "<span>Research Writing</span>\n",
            encoding="utf-8",
        )

        findings = _detect_fragile_icon_font_usage(str(repo_path))

        assert findings["issues"] == []
        assert findings["icon_tokens"] == []

    async def test_self_assess_fails_fragile_icon_font_usage(self, tmp_path):
        repo_path = tmp_path / "repo"
        src_path = repo_path / "src"
        src_path.mkdir(parents=True)
        (src_path / "Hero.jsx").write_text(
            "<span className=\"material-symbols-outlined\">arrow_forward</span>\n",
            encoding="utf-8",
        )

        class _MockRuntime:
            def run(self, prompt, **kw):
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 1.0,
                            "verdict": "pass",
                            "gaps": [],
                            "component_checks": [],
                            "criteria_checks": [],
                            "summary": "Looks good.",
                        }
                    )
                }

        state = {
            "_runtime": _MockRuntime(),
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": True},
            "changes_made": ["src/Hero.jsx"],
            "implementation_summary": "Added a hero section.",
            "test_results": {"passed": 8, "failed": 0},
        }

        result = await self_assess(state)

        assert result["route"] == "fail"
        assert result["self_assessment"]["verdict"] == "fail"
        assert any("inline SVG or a local React icon component" in gap for gap in result["self_assessment"]["gaps"])

    async def test_self_assess_raises_after_max_cycles(self, tmp_path):
        repo_path = tmp_path / "repo"
        src_path = repo_path / "src"
        src_path.mkdir(parents=True)
        (src_path / "Hero.jsx").write_text(
            "<span className=\"material-symbols-outlined\">arrow_forward</span>\n",
            encoding="utf-8",
        )

        class _MockRuntime:
            def run(self, prompt, **kw):
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 1.0,
                            "verdict": "pass",
                            "gaps": [],
                            "component_checks": [],
                            "criteria_checks": [],
                            "summary": "Looks good.",
                        }
                    )
                }

        state = {
            "_runtime": _MockRuntime(),
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": True},
            "changes_made": ["src/Hero.jsx"],
            "implementation_summary": "Added a hero section.",
            "test_results": {"passed": 8, "failed": 0},
            "assess_cycles": 2,
        }

        with pytest.raises(RuntimeError, match="self_assess failed after 3 cycles"):
            await self_assess(state)

    async def test_self_assess_retries_invalid_schema_same_cycle(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        class _MockRuntime:
            def __init__(self):
                self.calls = 0
                self.prompts = []

            def run(self, prompt, **kw):
                self.calls += 1
                self.prompts.append(prompt)
                if self.calls == 1:
                    return {"raw_response": json.dumps({"summary": "", "gaps": []})}
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 0.95,
                            "verdict": "pass",
                            "gaps": [],
                            "component_checks": [],
                            "criteria_checks": [],
                            "summary": "Looks good.",
                        }
                    )
                }

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": ["src/App.jsx"],
            "implementation_summary": "Implemented the page.",
            "test_results": {"passed": 4, "failed": 0},
        }

        result = await self_assess(state)

        assert runtime.calls == 2
        assert result["assess_cycles"] == 1
        assert result["route"] == "pass"
        assert "previous self-assessment response was invalid" in runtime.prompts[1]

    async def test_self_assess_filters_non_actionable_review_comments(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        report_path = tmp_path / "review-report.json"
        report_path.write_text(
            json.dumps(
                {
                    "comments": [
                        {
                            "severity": "high",
                            "file": "src/pages/__tests__/PracticeQuizPage.test.jsx",
                            "message": "Test file content is truncated in diff - actual test assertions not fully visible for review.",
                        },
                        {
                            "severity": "medium",
                            "file": "src/pages/PracticeQuizPage.jsx",
                            "message": "Real actionable issue.",
                        },
                        {
                            "severity": "low",
                            "category": "large-change",
                            "message": "Single-file change is very large.",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        class _MockRuntime:
            def __init__(self):
                self.prompt = ""

            def run(self, prompt, **kw):
                self.prompt = prompt
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 0.95,
                            "verdict": "pass",
                            "gaps": [],
                            "component_checks": [],
                            "criteria_checks": [],
                            "summary": "Looks good.",
                        }
                    )
                }

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "review_report_path": "review-report.json",
            "revision_feedback": "please address review comments",
            "definition_of_done": {"screenshot_required": False},
            "changes_made": ["src/pages/PracticeQuizPage.jsx"],
            "implementation_summary": "Adjusted the page.",
            "test_results": {"passed": 4, "failed": 0},
        }

        result = await self_assess(state)

        assert result["route"] == "pass"
        assert "Real actionable issue." in runtime.prompt
        assert "truncated in diff" not in runtime.prompt
        assert "Single-file change is very large." not in runtime.prompt


class TestWebDevNodes:

    async def test_setup_workspace_no_runtime(self, tmp_path):
        # Team Lead pre-clones the repo; setup_workspace verifies it exists.
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        # Minimal git init so branch operations work
        import subprocess
        subprocess.run(["git", "init", repo_path], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.email", "test@test.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.name", "Test"],
                       check=True, capture_output=True)
        # Create an initial commit so checkout -b works
        (tmp_path / "repo" / "README.md").write_text("hi")
        subprocess.run(["git", "-C", repo_path, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "commit", "-m", "init"],
                       check=True, capture_output=True)

        state = {
            "_task_id": "t-123",
            "repo_url": "https://github.com/org/repo",
            "repo_path": repo_path,
            "workspace_path": str(tmp_path),
            "branch_name": "fix/test",
        }
        from unittest.mock import patch, MagicMock
        with patch("agents.web_dev.nodes._call_boundary_tool", return_value={"branches": []}):
            result = await setup_workspace(state)
        assert "workspace_path" in result
        assert "repo_path" in result
        assert result["branch_created"] is True
        assert result["branch_name"] == "fix/test"

    async def test_setup_workspace_derives_branch_from_runtime(self, tmp_path):
        """When no branch_name is preset, runtime.run() is called for it."""
        import subprocess
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        subprocess.run(["git", "init", repo_path], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.email", "test@test.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.name", "Test"],
                       check=True, capture_output=True)
        (tmp_path / "repo" / "README.md").write_text("hi")
        subprocess.run(["git", "-C", repo_path, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "commit", "-m", "init"],
                       check=True, capture_output=True)

        class _MockRuntime:
            def run(self, prompt, **kw):
                return {"raw_response": '{"branch_name": "feature/ABC-1-login", "workspace_notes": "x"}'}

        from unittest.mock import patch
        state = {
            "_task_id": "t-1",
            "_runtime": _MockRuntime(),
            "repo_url": "http://repo",
            "repo_path": repo_path,
            "workspace_path": str(tmp_path),
            "user_request": "Add login",
        }
        with patch("agents.web_dev.nodes._call_boundary_tool", return_value={"branches": []}):
            result = await setup_workspace(state)
        assert result["branch_name"] == "feature/ABC-1-login"

    async def test_setup_workspace_suffixes_when_remote_branch_exists(self, tmp_path):
        import subprocess
        from unittest.mock import patch

        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        subprocess.run(["git", "init", repo_path], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.email", "test@test.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.name", "Test"],
                       check=True, capture_output=True)
        (tmp_path / "repo" / "README.md").write_text("hi")
        subprocess.run(["git", "-C", repo_path, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "commit", "-m", "init"],
                       check=True, capture_output=True)

        def _boundary(_state, tool_name, payload):
            if tool_name == "scm_list_branches":
                return {"branches": [{"displayId": "feature/CSTL-1-landing-page"}]}
            if tool_name == "scm_list_prs":
                return {"prs": []}
            return {}

        state = {
            "_task_id": "t-1",
            "repo_url": "https://github.com/org/repo",
            "repo_path": repo_path,
            "workspace_path": str(tmp_path),
            "branch_name": "feature/CSTL-1-landing-page",
        }
        with patch("agents.web_dev.nodes._call_boundary_tool", side_effect=_boundary):
            result = await setup_workspace(state)
        assert result["branch_name"] == "feature/CSTL-1-landing-page_2"

    async def test_setup_workspace_suffixes_when_open_pr_uses_branch(self, tmp_path):
        import subprocess
        from unittest.mock import patch

        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        subprocess.run(["git", "init", repo_path], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.email", "test@test.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.name", "Test"],
                       check=True, capture_output=True)
        (tmp_path / "repo" / "README.md").write_text("hi")
        subprocess.run(["git", "-C", repo_path, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "commit", "-m", "init"],
                       check=True, capture_output=True)

        def _boundary(_state, tool_name, payload):
            if tool_name == "scm_list_branches":
                return {"branches": []}
            if tool_name == "scm_list_prs":
                return {"prs": [{"fromBranch": "feature/CSTL-1-landing-page", "toBranch": "main"}]}
            return {}

        state = {
            "_task_id": "t-2",
            "repo_url": "https://github.com/org/repo",
            "repo_path": repo_path,
            "workspace_path": str(tmp_path),
            "branch_name": "feature/CSTL-1-landing-page",
        }
        with patch("agents.web_dev.nodes._call_boundary_tool", side_effect=_boundary):
            result = await setup_workspace(state)
        assert result["branch_name"] == "feature/CSTL-1-landing-page_2"

    async def test_setup_workspace_revision_mode_reuses_existing_branch(self, tmp_path):
        import subprocess
        from unittest.mock import patch

        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        subprocess.run(["git", "init", repo_path], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.email", "test@test.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.name", "Test"],
                       check=True, capture_output=True)
        (tmp_path / "repo" / "README.md").write_text("hi")
        subprocess.run(["git", "-C", repo_path, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "commit", "-m", "init"],
                       check=True, capture_output=True)
        default_branch = subprocess.run(
            ["git", "-C", repo_path, "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", repo_path, "checkout", "-b", "feature/reuse-me"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "checkout", default_branch],
                       check=True, capture_output=True)

        state = {
            "_task_id": "t-3",
            "repo_url": "https://github.com/org/repo",
            "repo_path": repo_path,
            "workspace_path": str(tmp_path),
            "revision_mode": True,
            "existing_branch": "feature/reuse-me",
        }

        with patch("agents.web_dev.nodes._call_boundary_tool", side_effect=AssertionError("revision mode should not query remote branch conflicts")):
            result = await setup_workspace(state)

        current_branch = subprocess.run(
            ["git", "-C", repo_path, "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert result["branch_name"] == "feature/reuse-me"
        assert current_branch == "feature/reuse-me"

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

    async def test_run_tests_with_passing_validation(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "agents.web_dev.nodes._run_mandatory_validation",
            lambda repo_path, workspace_path, cycle: {
                "install_ok": True,
                "build_ok": True,
                "test_ok": True,
                "passed": 5,
                "failed": 0,
                "output": "All good",
            },
        )

        state = {"_runtime": object(), "repo_path": str(tmp_path), "test_cycles": 0}
        result = await run_tests(state)
        assert result["route"] == "pass"

    async def test_run_tests_with_failing_validation(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "agents.web_dev.nodes._run_mandatory_validation",
            lambda repo_path, workspace_path, cycle: {
                "install_ok": True,
                "build_ok": False,
                "test_ok": False,
                "passed": 2,
                "failed": 3,
                "output": "FAILED",
            },
        )

        state = {"_runtime": object(), "repo_path": str(tmp_path), "test_cycles": 0}
        result = await run_tests(state)
        assert result["route"] == "fail"
        assert result["test_cycles"] == 1

    async def test_run_tests_max_cycles_fails_before_pr(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "agents.web_dev.nodes._run_mandatory_validation",
            lambda repo_path, workspace_path, cycle: {
                "install_ok": True,
                "build_ok": False,
                "test_ok": False,
                "passed": 0,
                "failed": 1,
                "output": "FAILED",
            },
        )

        state = {"_runtime": object(), "repo_path": str(tmp_path), "test_cycles": 2, "max_test_cycles": 3}
        with pytest.raises(RuntimeError, match="Mandatory validation failed"):
            await run_tests(state)

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

    async def test_capture_screenshot_requires_png_for_ui_task(self, tmp_path):
        state = {
            "_task_id": "task-123",
            "repo_path": str(tmp_path / "missing-repo"),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": True},
        }

        with pytest.raises(RuntimeError, match="Required UI screenshot capture failed"):
            await capture_screenshot(state)

    async def test_create_pr_no_runtime(self):
        state = {}
        result = await create_pr(state)
        assert "pr_url" in result

    async def test_create_pr_requires_screenshot_for_ui_task(self):
        class _MockRuntime:
            def run(self, prompt, **kw):
                return {"raw_response": '{"title": "Add UI", "description": "## Summary\\nAdded UI."}'}

        state = {
            "_runtime": _MockRuntime(),
            "definition_of_done": {"screenshot_required": True},
            "screenshot_captured": False,
        }

        with pytest.raises(RuntimeError, match="without captured PNG screenshots"):
            await create_pr(state)

    async def test_create_pr_with_runtime(self):
        from framework.tools.registry import get_registry
        from framework.tools.base import BaseTool, ToolResult
        from unittest.mock import patch

        class _MockSCMPush(BaseTool):
            name = "scm_push"
            description = "mock push"
            parameters_schema = {"type": "object", "properties": {}, "required": []}
            def execute_sync(self, **kw) -> ToolResult:
                return ToolResult(output='{"pushed": true}')

        class _MockSCMCreatePR(BaseTool):
            name = "scm_create_pr"
            description = "mock create pr"
            parameters_schema = {"type": "object", "properties": {}, "required": []}
            def execute_sync(self, **kw) -> ToolResult:
                return ToolResult(output='{"pr_url": "https://github.com/org/repo/pull/42", "pr_number": 42, "commit_hash": "abc123"}')

        registry = get_registry()
        registry.register(_MockSCMPush())
        registry.register(_MockSCMCreatePR())

        class _MockRuntime:
            def run(self, prompt, **kw):
                return {"raw_response": '{"title": "Add login page", "description": "## Summary\\nAdded login."}'}

        state = {
            "_runtime": _MockRuntime(),
            "user_request": "Add login",
            "branch_name": "feature/login",
            "repo_url": "https://github.com/org/repo",
            "repo_path": "/tmp/repo",
            "implementation_summary": "Added login form",
            "changes_made": ["src/login.py"],
            "jira_context": {"key": "ABC-123"},
        }
        with patch("agents.web_dev.nodes._git_commit_all_pending", return_value=["src/login.py"]), patch(
            "agents.web_dev.nodes._git_branch_changed_files",
            return_value=["package.json", "src/App.tsx", "src/login.py"],
        ):
            result = await create_pr(state)
        assert result["pr_url"] == "https://github.com/org/repo/pull/42"
        assert result["pr_title"] == "Add login page"
        assert result["changes_made"] == ["package.json", "src/App.tsx", "src/login.py"]

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

    async def test_happy_path_no_runtime(self, tmp_path):
        import subprocess
        from unittest.mock import patch
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        subprocess.run(["git", "init", repo_path], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.email", "t@t.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.name", "T"],
                       check=True, capture_output=True)
        (tmp_path / "repo" / "README.md").write_text("hi")
        subprocess.run(["git", "-C", repo_path, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "commit", "-m", "init"],
                       check=True, capture_output=True)

        compiled = web_dev_workflow.compile()
        state = {
            "_task_id": "test-123",
            "repo_url": "https://github.com/test",
            "repo_path": repo_path,
            "workspace_path": str(tmp_path),
            "branch_name": "fix/ABC-123",
            "analysis": "Fix the login button",
            "user_request": "Fix login",
            "test_cycles": 0,
        }
        with patch("agents.web_dev.nodes._call_boundary_tool", return_value={"branches": []}):
            result = await compiled.invoke(state)
        assert result["success"] is True
        assert result["state"] == "TASK_STATE_COMPLETED"

    async def test_workflow_state_keys_populated(self, tmp_path):
        import subprocess
        from unittest.mock import patch
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        subprocess.run(["git", "init", repo_path], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.email", "t@t.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "config", "user.name", "T"],
                       check=True, capture_output=True)
        (tmp_path / "repo" / "README.md").write_text("hi")
        subprocess.run(["git", "-C", repo_path, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "commit", "-m", "init"],
                       check=True, capture_output=True)

        compiled = web_dev_workflow.compile()
        state = {
            "_task_id": "t-1",
            "repo_path": repo_path,
            "workspace_path": str(tmp_path),
            "user_request": "Add feature",
        }
        with patch("agents.web_dev.nodes._call_boundary_tool", return_value={"branches": []}):
            result = await compiled.invoke(state)
        assert "workspace_path" in result
        assert "implementation_plan" in result
        assert "test_results" in result
        assert "success" in result


class TestWebDevBoundaryTools:

    def test_call_boundary_tool_forwards_parent_supplied_child_permissions(self, monkeypatch):
        captured = {}

        class FakeRegistry:
            def execute_sync(self, name, arguments):
                captured["name"] = name
                captured["arguments"] = arguments
                return "{}"

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: FakeRegistry())

        result = _call_boundary_tool(
            {
                "metadata": {
                    "permissions": {
                        "allowedTools": ["scm_push", "scm_create_pr"],
                        "deniedTools": [],
                        "scm": "read-write",
                        "filesystem": "workspace-only",
                        "custom": {},
                    }
                }
            },
            "scm_push",
            {"repo_path": "/tmp/repo", "branch": "feature/test"},
        )

        assert result == {}
        assert captured["name"] == "scm_push"
        assert captured["arguments"]["branch"] == "feature/test"
        assert captured["arguments"]["permissions"]["scm"] == "read-write"

    def test_call_boundary_tool_does_not_invent_permissions_when_parent_did_not_pass_any(self, monkeypatch):
        captured = {}

        class FakeRegistry:
            def execute_sync(self, name, arguments):
                captured["name"] = name
                captured["arguments"] = arguments
                return "{}"

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: FakeRegistry())

        result = _call_boundary_tool(
            {"metadata": {}},
            "scm_push",
            {"repo_path": "/tmp/repo", "branch": "feature/test"},
        )

        assert result == {}
        assert captured["name"] == "scm_push"
        assert captured["arguments"]["branch"] == "feature/test"
        assert "permissions" not in captured["arguments"]

    def test_jira_tools_accept_task_metadata(self, monkeypatch):
        from agents.web_dev.tools import JiraComment, JiraGetTokenUser, JiraListTransitions

        dispatched = []

        class StubRegistryClient:
            def discover(self, capability):
                return f"http://stub/{capability}"

        def _dispatch_sync(url, capability, message_parts, metadata, **kwargs):
            dispatched.append({
                "capability": capability,
                "metadata": metadata,
            })
            return {
                "task": {
                    "artifacts": [
                        {"parts": [{"text": json.dumps({"status": "ok"})}]}
                    ]
                }
            }

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        JiraGetTokenUser().execute_sync(task_id="task-123")
        JiraListTransitions().execute_sync(ticket_key="CSTL-2", task_id="task-123")
        JiraComment().execute_sync(ticket_key="CSTL-2", comment="picked up", task_id="task-123")

        assert dispatched[0]["metadata"]["task_id"] == "task-123"
        assert dispatched[1]["metadata"]["task_id"] == "task-123"
        assert dispatched[2]["metadata"]["task_id"] == "task-123"

    def test_register_web_dev_tools_includes_scm_tools(self):
        from framework.tools.registry import get_registry

        register_web_dev_tools()
        registry = get_registry()
        assert registry.get("scm_push") is not None
        assert registry.get("scm_create_pr") is not None
        assert registry.get("scm_upload_pr_image") is not None
        assert registry.get("scm_update_pr") is not None

    def test_scm_create_pr_derives_bitbucket_coordinates(self, monkeypatch):
        dispatched = {}

        class StubRegistryClient:
            def discover(self, capability):
                return f"http://stub/{capability}"

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

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        result = SCMCreatePR().execute_sync(
            repo_url="https://bitbucket.example.com/projects/PROJ/repos/web-ui-test/browse",
            source_branch="feature/proj-123",
            target_branch="main",
            title="PROJ-123: add login page",
            description="PR body",
            task_id="task-123",
        )

        payload = json.loads(result.output)
        assert payload["prUrl"] == "https://bitbucket/pr/42"
        assert dispatched["capability"] == "scm.pr.create"
        assert dispatched["metadata"]["project"] == "PROJ"
        assert dispatched["metadata"]["repo"] == "web-ui-test"
        assert dispatched["metadata"]["task_id"] == "task-123"

    def test_scm_upload_pr_image_derives_github_coordinates(self, monkeypatch, tmp_path):
        image_file = tmp_path / "screen.png"
        image_file.write_bytes(b"png")
        dispatched = {}

        class StubRegistryClient:
            def discover(self, capability):
                return f"http://stub/{capability}"

        def _dispatch_sync(url, capability, message_parts, metadata, **kwargs):
            dispatched["capability"] = capability
            dispatched["metadata"] = metadata
            return {
                "task": {
                    "artifacts": [
                        {"parts": [{"text": json.dumps({"ok": True, "image_url": "https://cdn.example/screen.png"})}]}
                    ]
                }
            }

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        result = SCMUploadPRImage().execute_sync(
            repo_url="https://github.com/org/repo",
            image_path=str(image_file),
            pr_number=42,
            task_id="task-123",
        )

        payload = json.loads(result.output)
        assert payload["image_url"] == "https://cdn.example/screen.png"
        assert dispatched["capability"] == "scm.pr.image.upload"
        assert dispatched["metadata"]["project"] == "org"
        assert dispatched["metadata"]["repo"] == "repo"
        assert dispatched["metadata"]["imagePath"] == str(image_file)
        assert dispatched["metadata"]["task_id"] == "task-123"

