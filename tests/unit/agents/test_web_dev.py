"""Tests for Web Dev Agent workflow."""
import json
import os
from pathlib import Path
import pytest
from unittest.mock import MagicMock
from framework.agent import AgentServices
from framework.task_store import InMemoryTaskStore
from framework.workflow import START, END
from agents.web_dev.agent import WebDevAgent, web_dev_workflow, web_dev_definition
from agents.web_dev.tools import SCMCreatePR, SCMUploadPRImage, register_web_dev_tools
from agents.web_dev.nodes import (
    prepare_jira,
    setup_workspace,
    analyze_task,
    implement_changes,
    run_tests,
    fix_tests,
    fix_gaps,
    self_assess,
    capture_screenshot,
    create_pr,
    report_result,
    _call_boundary_tool,
    _safe_json,
    _detect_fragile_icon_font_usage,
    _is_implementation_ground_truth_present,
    _self_assessment_claims_conflict_with_ground_truth,
    _bootstrap_minimal_frontend_scaffold,
    _build_self_assessment_source_evidence,
    _expand_changed_source_files,
    _repo_has_frontend_entrypoint,
    _try_ground_truth_re_prompt,
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


def _timeline_task_store(task_id: str = "task-major-steps"):
    store = InMemoryTaskStore()
    store.create_task(agent_id="web-dev", task_id=task_id)
    return store


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

    def test_web_dev_declares_single_shot_and_agentic_runtime_capabilities(self):
        assert web_dev_definition.runtime_capabilities["run"] is True
        assert web_dev_definition.runtime_capabilities["run_agentic"] is True
        assert web_dev_definition.runtime_capabilities["agentic_tools"] is True


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


class TestWebDevBoundaries:

    def test_web_dev_does_not_import_code_review_agent_modules(self):
        repo_root = Path(__file__).resolve().parents[3]
        nodes_source = (repo_root / "agents" / "web_dev" / "nodes.py").read_text(encoding="utf-8")
        prompts_source = (repo_root / "agents" / "web_dev" / "prompts" / "__init__.py").read_text(encoding="utf-8")

        assert "from agents.code_review" not in nodes_source
        assert "from agents.code_review" not in prompts_source


class TestWebDevMajorSteps:
    async def test_analyze_task_records_drafting_plan_row(self, tmp_path):
        store = _timeline_task_store()
        result = await analyze_task(
            {
                "_task_id": "task-major-steps",
                "_task_store": store,
                "workspace_path": str(tmp_path),
                "analysis": "Implement the task",
            }
        )

        row = store.get_task("task-major-steps").metadata["major_step_rows"]["wd.drafting_plan#0"]
        assert row["title"] == "Web Dev drafting plan"
        assert row["lifecycle_state"] == "done"
        assert result["implementation_plan"]

    async def test_run_tests_no_runtime_records_building_row(self):
        store = _timeline_task_store()
        result = await run_tests(
            {
                "_task_id": "task-major-steps",
                "_task_store": store,
                "_runtime": None,
            }
        )

        row = store.get_task("task-major-steps").metadata["major_step_rows"]["wd.building#0"]
        assert row["title"] == "Web Dev building and testing"


class TestWebDevJiraPrivacy:
    async def test_prepare_jira_redacts_personal_identifiers_in_logs_and_artifacts(self, tmp_path, monkeypatch):
        boundary_calls = []

        def _boundary(_state, tool_name, payload):
            boundary_calls.append((tool_name, payload))
            if tool_name == "jira_get_token_user":
                return {
                    "user": {
                        "emailAddress": "person@example.com",
                        "accountId": "acct-123456",
                    }
                }
            if tool_name == "jira_list_transitions":
                return {"transitions": [{"name": "In Progress"}]}
            return {}

        monkeypatch.setattr("agents.web_dev.nodes._call_boundary_tool", _boundary)

        state = {
            "_task_id": "task-privacy",
            "workspace_path": str(tmp_path),
            "jira_context": {
                "key": "CSTL-1",
                "fields": {
                    "status": {"name": "To Do"},
                    "assignee": {"emailAddress": "owner@example.com"},
                },
            },
        }

        result = await prepare_jira(state)

        comment_payload = next(payload for name, payload in boundary_calls if name == "jira_comment")
        assert "person@example.com" not in comment_payload["comment"]
        assert "owner@example.com" not in comment_payload["comment"]
        assert "Assignee: token user" in comment_payload["comment"]

        log_payload = json.loads((tmp_path / "web-dev" / "jira-prepare-log.json").read_text(encoding="utf-8"))
        data = log_payload["data"]
        assert data["jira_original_assignee"] == "redacted"
        assert data["jira_original_assignee_present"] is True
        assert data["jira_token_user"] == "redacted"
        assert data["jira_token_user_present"] is True

        assert result["jira_original_assignee"] == "redacted"
        assert result["jira_token_user"] == "redacted"

    async def test_self_assess_no_runtime_records_self_check_row(self):
        store = _timeline_task_store()
        result = await self_assess(
            {
                "_task_id": "task-major-steps",
                "_task_store": store,
                "_runtime": None,
            }
        )

        row = store.get_task("task-major-steps").metadata["major_step_rows"]["wd.self_check#0"]
        assert row["title"] == "Web Dev running self-check"
        assert row["lifecycle_state"] == "done"
        assert result["route"] == "pass"

    async def test_self_assess_uses_cumulative_branch_and_worktree_changes(
        self, tmp_path
    ):
        """Self-assessment evidence must include committed implementation
        files plus later uncommitted validation/test fixes.
        """
        import subprocess

        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", *args],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test User")
        (repo_path / "README.md").write_text("# Example\n", encoding="utf-8")
        git("add", "README.md")
        git("commit", "-m", "initial")
        git("checkout", "-b", "feature/task")

        app_path = repo_path / "src" / "App.tsx"
        page_path = repo_path / "src" / "pages" / "LandingPage.tsx"
        page_path.parent.mkdir(parents=True)
        app_path.write_text(
            "import LandingPage from './pages/LandingPage'\n"
            "export default function App() { return <LandingPage /> }\n",
            encoding="utf-8",
        )
        page_path.write_text(
            "export default function LandingPage() { return <main>Done</main> }\n",
            encoding="utf-8",
        )
        git("add", "src/App.tsx", "src/pages/LandingPage.tsx")
        git("commit", "-m", "implement feature")

        (repo_path / "package.json").write_text(
            '{"scripts":{"test":"vitest"}}\n',
            encoding="utf-8",
        )
        test_path = repo_path / "src" / "pages" / "LandingPage.test.tsx"
        test_path.write_text("test('renders', () => {})\n", encoding="utf-8")

        class _PromptCapturingRuntime:
            def __init__(self) -> None:
                self.prompt = ""

            def run(self, prompt, **kw):
                self.prompt = prompt
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 0.95,
                            "verdict": "pass",
                            "criteria_checks": [],
                            "component_checks": [],
                            "self_review_issues": [],
                            "gaps": [],
                            "summary": "All checks passed.",
                        }
                    )
                }

        runtime = _PromptCapturingRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": [],
            "implementation_summary": "Implemented feature and test fixes.",
            "test_results": {
                "passed": 2,
                "failed": 0,
                "build_ok": True,
                "test_ok": True,
            },
        }

        result = await self_assess(state)

        assert result["route"] == "pass"
        assert "src/App.tsx" in runtime.prompt
        assert "src/pages/LandingPage.tsx" in runtime.prompt
        assert "src/pages/LandingPage.test.tsx" in runtime.prompt
        assert "package.json" in runtime.prompt
        assert state["changes_made"] == []

    async def test_report_result_records_handover_row(self):
        store = _timeline_task_store()
        result = await report_result(
            {
                "_task_id": "task-major-steps",
                "_task_store": store,
                "pr_url": "https://example.com/pull/42",
                "branch_name": "feature/cstl-1",
                "changes_made": ["app.py"],
                "test_status": "pass",
                "pr_title": "Implement feature",
            }
        )

        row = store.get_task("task-major-steps").metadata["major_step_rows"]["wd.handover#0"]
        assert row["title"] == "Web Dev handing over to Team Lead"
        assert row["lifecycle_state"] == "done"
        assert result["success"] is True


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

    def test_detect_fragile_icon_font_usage_ignores_unused_material_icon_css(self, tmp_path):
        repo_path = tmp_path / "repo"
        src_path = repo_path / "src"
        src_path.mkdir(parents=True)
        (src_path / "index.css").write_text(
            ".material-symbols-outlined {\n"
            "  font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;\n"
            "}\n",
            encoding="utf-8",
        )
        (src_path / "ArrowIcon.tsx").write_text(
            "export default function ArrowIcon() {\n"
            "  return <svg viewBox=\"0 0 24 24\"><path d=\"M12 4l8 8-8 8\" /></svg>\n"
            "}\n",
            encoding="utf-8",
        )

        findings = _detect_fragile_icon_font_usage(str(repo_path))

        assert findings["issues"] == []
        assert findings["uses_material_icon_class"] is False
        assert findings["icon_tokens"] == []

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

    def test_detect_fragile_icon_font_usage_ignores_svg_replacement_comment(self, tmp_path):
        repo_path = tmp_path / "repo"
        src_path = repo_path / "src"
        src_path.mkdir(parents=True)
        (src_path / "ArrowForwardIcon.tsx").write_text(
            "/**\n"
            " * Inline SVG replacement for Material Symbols Outlined arrow_forward.\n"
            " */\n"
            "export default function ArrowForwardIcon() {\n"
            "  return <svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M5 12h14\" /></svg>\n"
            "}\n",
            encoding="utf-8",
        )

        findings = _detect_fragile_icon_font_usage(str(repo_path))

        assert findings["issues"] == []
        assert findings["files"] == []
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

    async def test_self_assess_max_cycle_failed_reprompt_still_fails_task(self, tmp_path):
        repo_path = tmp_path / "repo"
        src_path = repo_path / "src"
        src_path.mkdir(parents=True)
        (src_path / "App.jsx").write_text("export default function App() { return <main /> }\n", encoding="utf-8")

        class _MockRuntime:
            def __init__(self):
                self.calls = 0

            def run(self, prompt, **kw):
                self.calls += 1
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 0.85 if self.calls == 1 else 0.89,
                            "verdict": "fail",
                            "gaps": ["Remaining implementation gap."],
                            "component_checks": [],
                            "criteria_checks": [],
                            "self_review_issues": [],
                            "summary": "Still has a gap.",
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
            "implementation_summary": "Implemented the app.",
            "test_results": {"build_ok": True, "test_ok": True, "passed": 8, "failed": 0},
            "assess_cycles": 2,
        }

        with pytest.raises(RuntimeError, match="self_assess failed after 3 cycles"):
            await self_assess(state)
        assert runtime.calls == 1

    async def test_self_assess_reprompt_cannot_upgrade_failed_assessment_to_pass(self, tmp_path):
        repo_path = tmp_path / "repo"
        src_path = repo_path / "src"
        src_path.mkdir(parents=True)
        (src_path / "App.jsx").write_text("export default function App() { return <main /> }\n", encoding="utf-8")

        class _MockRuntime:
            def __init__(self):
                self.calls = 0

            def run(self, prompt, **kw):
                self.calls += 1
                if self.calls == 1:
                    payload = {
                        "score": 0.85,
                        "verdict": "fail",
                        "gaps": ["Remaining implementation gap."],
                        "component_checks": [],
                        "criteria_checks": [],
                        "self_review_issues": [],
                        "summary": "Still has a gap.",
                    }
                else:
                    payload = {
                        "score": 1.0,
                        "verdict": "pass",
                        "gaps": [],
                        "component_checks": [],
                        "criteria_checks": [],
                        "self_review_issues": [],
                        "summary": "Re-prompt says everything is complete.",
                    }
                return {"raw_response": json.dumps(payload)}

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": ["src/App.jsx"],
            "implementation_summary": "Implemented the app.",
            "test_results": {"build_ok": True, "test_ok": True, "passed": 8, "failed": 0},
            "assess_cycles": 2,
        }

        with pytest.raises(RuntimeError, match="self_assess failed after 3 cycles"):
            await self_assess(state)
        assert runtime.calls == 1

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

    async def test_self_assess_invalid_schema_does_not_route_to_fix_gaps(self, tmp_path, monkeypatch):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        monkeypatch.setenv("WEB_DEV_SELF_ASSESS_SCHEMA_ATTEMPTS", "2")

        class _MockRuntime:
            def __init__(self):
                self.calls = 0

            def run(self, prompt, **kw):
                self.calls += 1
                return {"raw_response": json.dumps({"summary": "No structured verdict."})}

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
        assert result["route"] == "need_user_input"
        assert result["self_assessment"]["failure_type"] == "schema"
        assert result["self_assessment"]["verdict"] == "error"
        assert "Self-assessment output invalid" in result["self_assessment"]["summary"]

    async def test_self_assess_agentic_file_fallback_succeeds(self, tmp_path, monkeypatch):
        """Text-mode returns junk for every attempt, but the agentic fallback
        writes a valid JSON file and the node recovers.

        This is the core methodology fix: when any agentic CLI backend
        (copilot-cli, claude-code, codex-cli) keeps returning unparseable
        text, the node delegates to ``run_agentic`` with cwd=workspace so
        the LLM can write the structured JSON to disk directly.
        """
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        monkeypatch.setenv("WEB_DEV_SELF_ASSESS_SCHEMA_ATTEMPTS", "2")

        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def __init__(self) -> None:
                self.run_calls = 0
                self.run_agentic_calls = 0
                self.run_agentic_kwargs: dict = {}

            def run(self, prompt, **kw):
                self.run_calls += 1
                # Always return unparseable text — no JSON, no score/verdict.
                return {"raw_response": "<think>The model never emits valid JSON here.</think>"}

            def run_agentic(self, task, **kw):
                self.run_agentic_calls += 1
                self.run_agentic_kwargs = kw
                # Write a valid self-assessment JSON to the path mentioned in
                # the task prompt — that path is deterministic.
                import re as _re
                match = _re.search(r"Target file:\s*(\S+)", task)
                assert match, "Fallback task prompt must include 'Target file: <path>'"
                target = match.group(1)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                payload = {
                    "score": 0.94,
                    "verdict": "pass",
                    "criteria_checks": [{"criterion": "X", "status": "pass", "notes": ""}],
                    "component_checks": [],
                    "self_review_issues": [],
                    "gaps": [],
                    "summary": "All good.",
                }
                with open(target, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                return AgenticResult(success=True, summary=target, backend_used="mock")

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": ["src/App.jsx"],
            "implementation_summary": "Implemented.",
            "test_results": {"passed": 1, "failed": 0},
        }

        result = await self_assess(state)

        # Text path was attempted (2 attempts) and then the fallback ran once.
        assert runtime.run_calls == 2
        assert runtime.run_agentic_calls == 1
        # The fallback must have been invoked with a cwd (every backend needs it).
        assert runtime.run_agentic_kwargs.get("cwd")
        # The validated payload from the file is what we report.
        assert result["self_assessment"]["score"] == 0.94
        assert result["self_assessment"]["verdict"] == "pass"
        assert result["route"] == "pass"
        # The file the fallback wrote is persisted under the workspace.
        assert (tmp_path / "web-dev" / "self-assessment-llm-1.json").exists()

    async def test_self_assess_agentic_file_fallback_invalid_escalates(self, tmp_path, monkeypatch):
        """Fallback path writes a file but its content still lacks score/verdict.

        The node must escalate exactly as if no fallback had been attempted —
        verdict='error', failure_type='schema', route='need_user_input'.
        """
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        monkeypatch.setenv("WEB_DEV_SELF_ASSESS_SCHEMA_ATTEMPTS", "1")

        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def run(self, prompt, **kw):
                return {"raw_response": "not json at all"}

            def run_agentic(self, task, **kw):
                import re as _re
                match = _re.search(r"Target file:\s*(\S+)", task)
                target = match.group(1) if match else ""
                os.makedirs(os.path.dirname(target), exist_ok=True)
                # File is written but contains a dict missing the required keys.
                with open(target, "w", encoding="utf-8") as fh:
                    json.dump({"summary": "I forgot the score and verdict."}, fh)
                return AgenticResult(success=True, summary="done", backend_used="mock")

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": ["src/App.jsx"],
            "implementation_summary": "Implemented.",
            "test_results": {"passed": 1, "failed": 0},
        }

        result = await self_assess(state)

        assert result["route"] == "need_user_input"
        assert result["self_assessment"]["verdict"] == "error"
        assert result["self_assessment"]["failure_type"] == "schema"
        # The escalation gap should mention both text-mode AND the fallback,
        # so the operator can see both code paths were exhausted.
        gap0 = result["self_assessment"]["gaps"][0]
        assert "agentic-file fallback" in gap0

    async def test_self_assess_agentic_file_fallback_passes_with_non_blocking_issues(
        self, tmp_path, monkeypatch
    ):
        """Advisory self-review issues must not turn a passing run into a fail.

        The text-mode path keeps producing unparseable output (parser brittleness
        we cannot fix in user code), but the agentic-file fallback succeeds and
        reports a single non-blocking issue. The validator must accept this
        because the model explicitly marked the issue as ``blocking: false``.
        """
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        monkeypatch.setenv("WEB_DEV_SELF_ASSESS_SCHEMA_ATTEMPTS", "1")

        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def run(self, prompt, **kw):
                # Text mode never produces parseable output for this case.
                return {"raw_response": "think block only, no JSON"}

            def run_agentic(self, task, **kw):
                import re as _re
                match = _re.search(r"Target file:\s*(\S+)", task)
                target = match.group(1) if match else ""
                os.makedirs(os.path.dirname(target), exist_ok=True)
                payload = {
                    "score": 0.95,
                    "verdict": "pass",
                    "criteria_checks": [{"criterion": "X", "status": "pass", "notes": ""}],
                    "component_checks": [],
                    "self_review_issues": [
                        {
                            "severity": "low",
                            "message": "Cosmetic polish: tweak footer spacing.",
                            "blocking": False,
                        }
                    ],
                    "gaps": [],
                    "summary": "Implementation is functionally complete.",
                }
                with open(target, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                return AgenticResult(success=True, summary=target, backend_used="mock")

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": ["src/App.jsx"],
            "implementation_summary": "Implemented.",
            "test_results": {"passed": 1, "failed": 0},
        }

        result = await self_assess(state)

        # The fallback was used, the validator accepted the non-blocking
        # advisory issue, and the node routes to pass.
        assert result["route"] == "pass"
        assert result["self_assessment"]["verdict"] == "pass"
        assert result["self_assessment"]["score"] == 0.95
        # failure_type is only set when the schema failed; a successful pass
        # must NOT have it.
        assert result["self_assessment"].get("failure_type") != "schema"

    async def test_self_assess_error_message_uses_fallback_feedback_when_fallback_fails(
        self, tmp_path, monkeypatch
    ):
        """When both text-mode and the agentic-file fallback fail, the user
        error message must reflect the *fallback* failure, not the stale
        text-mode feedback from earlier attempts.
        """
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        monkeypatch.setenv("WEB_DEV_SELF_ASSESS_SCHEMA_ATTEMPTS", "1")

        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def run(self, prompt, **kw):
                # Text mode always claims it is missing required fields.
                return {"raw_response": "nope, just prose, no json"}

            def run_agentic(self, task, **kw):
                import re as _re
                match = _re.search(r"Target file:\s*(\S+)", task)
                target = match.group(1) if match else ""
                os.makedirs(os.path.dirname(target), exist_ok=True)
                # The agent writes valid JSON but it trips a different
                # consistency rule than the text-mode one did.
                payload = {
                    "score": 0.95,
                    "verdict": "pass",
                    "criteria_checks": [],
                    "component_checks": [],
                    "self_review_issues": [
                        {
                            "severity": "high",
                            "message": "Blocking issue the model forgot to label.",
                            "blocking": True,
                        }
                    ],
                    "gaps": [],
                    "summary": "Failed consistency check.",
                }
                with open(target, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                return AgenticResult(success=True, summary=target, backend_used="mock")

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": ["src/App.jsx"],
            "implementation_summary": "Implemented.",
            "test_results": {"passed": 1, "failed": 0},
        }

        result = await self_assess(state)

        # Both paths failed. The user-facing error must reference the
        # *fallback* feedback (the most recent, the most relevant one), not
        # the stale text-mode "missing required fields" message.
        assert result["route"] == "need_user_input"
        assert result["self_assessment"]["failure_type"] == "schema"
        gap0 = result["self_assessment"]["gaps"][0]
        schema_feedback = result["self_assessment"]["schema_feedback"]
        assert "blocking issue" in gap0, gap0
        assert "blocking issue" in schema_feedback, schema_feedback
        # Sanity: it must not still report the older "missing required fields"
        # feedback which was the text-mode parser failure.
        assert "missing required fields" not in gap0, gap0

    async def test_self_assess_agentic_fallback_disabled_by_env(self, tmp_path, monkeypatch):
        """``WEB_DEV_SELF_ASSESS_AGENTIC_FALLBACK=0`` opts out of the fallback.

        Operators that want the legacy "text-only" behaviour for debugging
        must still be able to disable the file path.
        """
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        monkeypatch.setenv("WEB_DEV_SELF_ASSESS_SCHEMA_ATTEMPTS", "1")
        monkeypatch.setenv("WEB_DEV_SELF_ASSESS_AGENTIC_FALLBACK", "0")

        class _MockRuntime:
            def __init__(self) -> None:
                self.run_agentic_calls = 0

            def run(self, prompt, **kw):
                return {"raw_response": "not json"}

            def run_agentic(self, task, **kw):
                self.run_agentic_calls += 1
                raise AssertionError("run_agentic must not be called when fallback is disabled")

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": ["src/App.jsx"],
            "implementation_summary": "Implemented.",
            "test_results": {"passed": 1, "failed": 0},
        }

        result = await self_assess(state)

        assert runtime.run_agentic_calls == 0
        assert result["route"] == "need_user_input"
        assert result["self_assessment"]["failure_type"] == "schema"

    async def test_self_assess_recovers_from_max_cycles_hallucination(
        self, tmp_path, monkeypatch
    ):
        """The model hallucinated that the implementation is missing on the
        final cycle, but the build and tests pass and the actual files exist.
        The ground-truth re-prompt must catch the contradiction and recover.
        """
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        # Create a non-trivial file that the model might claim is missing.
        impl_file = repo_path / "src" / "pages" / "FeaturePage.jsx"
        impl_file.parent.mkdir(parents=True, exist_ok=True)
        impl_file.write_text(
            "export default function FeaturePage() { /* real content */ }\n"
            * 50,
            encoding="utf-8",
        )
        # Force the loop to run only one attempt so we hit the
        # max_cycles-with-verdict=fail path on the first try.
        monkeypatch.setenv("WEB_DEV_SELF_ASSESS_SCHEMA_ATTEMPTS", "1")

        class _MockRuntime:
            def __init__(self) -> None:
                self.run_calls: list[str] = []

            def run(self, prompt, **kw):
                self.run_calls.append(prompt)
                if len(self.run_calls) == 1:
                    # First call: the model hallucinates that the page is
                    # missing. The file name it cites (FeaturePage.jsx) is
                    # the actual file on disk, so the contradiction can
                    # be detected by checking the filesystem.
                    return {
                        "raw_response": json.dumps(
                            {
                                "score": 0.55,
                                "verdict": "fail",
                                "criteria_checks": [
                                    {"criterion": "Implement the new feature page",
                                     "status": "fail",
                                     "notes": "Page not implemented."},
                                ],
                                "component_checks": [
                                    {"component": "TopNavBar", "status": "missing",
                                     "notes": "Navigation bar not present."},
                                ],
                                "self_review_issues": [
                                    {
                                        "severity": "critical",
                                        "file": "src/pages/FeaturePage.jsx",
                                        "message": "FeaturePage.jsx does not exist; no component was created.",
                                        "blocking": True,
                                    },
                                ],
                                "gaps": [
                                    "No FeaturePage.jsx component file created",
                                ],
                                "summary": "Page not implemented.",
                            }
                        )
                    }
                # Second call: the re-prompt with ground truth. The model
                # reads the file, realises it exists, and re-assesses as pass.
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 0.95,
                            "verdict": "pass",
                            "criteria_checks": [
                                {"criterion": "Implement the new feature page",
                                 "status": "pass",
                                 "notes": "Page implemented and tested."},
                            ],
                            "component_checks": [
                                {"component": "TopNavBar", "status": "present",
                                 "notes": "OK."},
                            ],
                            "self_review_issues": [],
                            "gaps": [],
                            "summary": "Implementation complete.",
                        }
                    )
                }

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": ["src/pages/FeaturePage.jsx", "src/App.jsx"],
            "implementation_summary": "Implemented the new feature page.",
            "test_results": {
                "passed": 9,
                "failed": 0,
                "build_ok": True,
                "test_ok": True,
            },
            "assess_cycles": 2,  # forces the max-cycles-with-verdict=fail path
        }

        result = await self_assess(state)

        # The ground-truth re-prompt should have fired and recovered the
        # assessment. The final route must be 'pass', not a RuntimeError.
        assert result["route"] == "pass", result
        assert result["self_assessment"]["verdict"] == "pass"
        assert result["self_assessment"]["score"] == 0.95
        assert result["self_assessment"]["ground_truth_reprompt"] == {
            "applied": True,
            "original_score": 0.55,
            "original_verdict": "fail",
            "reason": (
                "self_review_issues claim these files are missing/not implemented, "
                "but they exist on disk: ['src/pages/FeaturePage.jsx']"
            ),
        }
        assert len(runtime.run_calls) == 2
        # The second prompt must surface the ground-truth context so the
        # model is forced to re-evaluate.
        second_prompt = runtime.run_calls[1]
        assert "GROUND TRUTH" in second_prompt
        assert "9 passed" in second_prompt
        assert "FeaturePage.jsx" in second_prompt  # the real file is named

    async def test_self_assess_max_cycles_still_raises_when_ground_truth_missing(
        self, tmp_path, monkeypatch
    ):
        """When the implementation is *actually* missing (tests/build broken,
        no files changed) we must still raise — the ground-truth re-prompt is
        for catching hallucinations, not for papering over real failures.
        """
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        monkeypatch.setenv("WEB_DEV_SELF_ASSESS_SCHEMA_ATTEMPTS", "1")

        class _MockRuntime:
            def run(self, prompt, **kw):
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 0.0,
                            "verdict": "fail",
                            "criteria_checks": [],
                            "component_checks": [],
                            "self_review_issues": [
                                {
                                    "severity": "critical",
                                    "file": "src/missing.py",
                                    "message": "missing.py is missing; not implemented.",
                                    "blocking": True,
                                }
                            ],
                            "gaps": ["src/missing.py is missing"],
                            "summary": "Implementation is genuinely broken.",
                        }
                    )
                }

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": [],
            "implementation_summary": "Could not implement.",
            "test_results": {
                "passed": 0,
                "failed": 4,
                "build_ok": False,
                "test_ok": False,
            },
            "assess_cycles": 2,  # forces the max-cycles-with-verdict=fail path
        }

        with pytest.raises(RuntimeError) as excinfo:
            await self_assess(state)
        assert "self_assess failed" in str(excinfo.value)

    async def test_is_implementation_ground_truth_present(self):
        assert _is_implementation_ground_truth_present({
            "test_results": {"passed": 5, "failed": 0, "build_ok": True, "test_ok": True},
            "changes_made": ["a.py", "b.py"],
        }) is True
        # No tests passed → not present.
        assert _is_implementation_ground_truth_present({
            "test_results": {"passed": 0, "failed": 1, "build_ok": True, "test_ok": True},
            "changes_made": ["a.py"],
        }) is False
        # Build failed → not present.
        assert _is_implementation_ground_truth_present({
            "test_results": {"passed": 5, "failed": 0, "build_ok": False, "test_ok": True},
            "changes_made": ["a.py"],
        }) is False
        # No changes_made → not present.
        assert _is_implementation_ground_truth_present({
            "test_results": {"passed": 5, "failed": 0, "build_ok": True, "test_ok": True},
            "changes_made": [],
        }) is False
        # Missing keys → not present.
        assert _is_implementation_ground_truth_present({}) is False

    async def test_self_assess_source_evidence_expands_changed_directories(self, tmp_path):
        repo_path = tmp_path / "repo"
        (repo_path / "src" / "pages").mkdir(parents=True)
        (repo_path / "src" / "App.tsx").write_text(
            "import { Route } from 'react-router-dom';\n"
            "export default function App() { return <Route path=\"/quiz\" />; }\n",
            encoding="utf-8",
        )
        (repo_path / "src" / "pages" / "PracticeQuizPage.tsx").write_text(
            "export default function PracticeQuizPage() { return <main>Quiz</main>; }\n",
            encoding="utf-8",
        )
        (repo_path / "node_modules" / "pkg").mkdir(parents=True)
        (repo_path / "node_modules" / "pkg" / "index.js").write_text("ignore me", encoding="utf-8")
        (repo_path / "dist").mkdir()
        (repo_path / "dist" / "index.html").write_text("ignore me", encoding="utf-8")

        expanded = _expand_changed_source_files(str(repo_path), ["src/", "node_modules/", "dist/"])
        evidence = _build_self_assessment_source_evidence(str(repo_path), ["src/", "node_modules/", "dist/"])

        assert "src/App.tsx" in expanded
        assert "src/pages/PracticeQuizPage.tsx" in expanded
        assert not any(path.startswith("node_modules/") for path in expanded)
        assert not any(path.startswith("dist/") for path in expanded)
        assert "--- src/App.tsx ---" in evidence
        assert 'path="/quiz"' in evidence

    async def test_self_assess_prompt_includes_source_evidence_for_changed_directories(
        self, tmp_path
    ):
        repo_path = tmp_path / "repo"
        (repo_path / "src" / "pages").mkdir(parents=True)
        (repo_path / "src" / "App.tsx").write_text(
            "import PracticeQuizPage from './pages/PracticeQuizPage';\n"
            "export default function App() { return <PracticeQuizPage />; }\n",
            encoding="utf-8",
        )
        (repo_path / "src" / "pages" / "PracticeQuizPage.tsx").write_text(
            "export default function PracticeQuizPage() { return <main>Quiz</main>; }\n",
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
                            "criteria_checks": [],
                            "component_checks": [],
                            "self_review_issues": [],
                            "gaps": [],
                            "summary": "Source evidence verified.",
                        }
                    )
                }

        runtime = _MockRuntime()
        result = await self_assess(
            {
                "_runtime": runtime,
                "repo_path": str(repo_path),
                "workspace_path": str(tmp_path),
                "definition_of_done": {"screenshot_required": False},
                "changes_made": ["src/"],
                "implementation_summary": "Implemented page.",
                "test_results": {"passed": 3, "failed": 0, "build_ok": True, "test_ok": True},
            }
        )

        assert result["route"] == "pass"
        assert "Deterministic source evidence" in runtime.prompt
        assert "--- src/App.tsx ---" in runtime.prompt
        assert "PracticeQuizPage" in runtime.prompt

    async def test_minimal_frontend_scaffold_creates_entrypoint_without_overwriting(
        self, tmp_path
    ):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "package.json").write_text('{"name":"existing"}\n', encoding="utf-8")

        created = _bootstrap_minimal_frontend_scaffold(
            str(repo_path),
            {"tech_stack": ["react", "vite"], "user_request": "Implement a page"},
        )
        created_again = _bootstrap_minimal_frontend_scaffold(
            str(repo_path),
            {"tech_stack": ["react", "vite"], "user_request": "Implement a page"},
        )

        assert _repo_has_frontend_entrypoint(str(repo_path)) is True
        assert "src/App.tsx" in created
        assert "src/main.tsx" in created
        assert "package.json" not in created
        assert (repo_path / "package.json").read_text(encoding="utf-8") == '{"name":"existing"}\n'
        assert created_again == []

    async def test_implement_changes_prepares_frontend_scaffold_for_greenfield_repo(
        self, tmp_path, monkeypatch
    ):
        from framework.runtime.adapter import AgenticCapabilities, AgenticResult
        from framework.validation_gates import ValidationResult

        monkeypatch.setattr(
            "framework.validation_gates.validate_files_changed",
            lambda repo_path: ValidationResult(True, "files_changed"),
        )

        class _MockRuntime:
            def __init__(self):
                self.task = ""

            def agentic_capabilities(self):
                return AgenticCapabilities(
                    backend="copilot-cli",
                    agentic=True,
                    constellation_tools=True,
                    allowed_tools=True,
                    cwd=True,
                )

            def run_agentic(self, task, **kw):
                self.task = task
                return AgenticResult(success=True, summary="Implemented page", backend_used="copilot-cli")

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        runtime = _MockRuntime()

        result = await implement_changes(
            {
                "_runtime": runtime,
                "user_request": "Implement a React page",
                "implementation_plan": "Build the UI",
                "repo_path": str(repo_path),
                "workspace_path": str(tmp_path),
                "branch_name": "feature/frontend",
                "tech_stack": ["react", "vite"],
                "_allowed_tools": ["read_file", "write_file", "run_command"],
            }
        )

        assert result["agentic_success"] is True
        assert (repo_path / "src" / "App.tsx").is_file()
        assert (repo_path / "src" / "main.tsx").is_file()
        assert "Workflow scaffold note" in runtime.task
        assert "src/App.tsx" in runtime.task
        assert "Replace the placeholder" in runtime.task

    async def test_self_assessment_claims_conflict_with_ground_truth(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        real_file = repo_path / "src" / "x.jsx"
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.write_text("x" * 200, encoding="utf-8")

        # Model claims the file is missing — contradicts reality.
        conflict, reason = _self_assessment_claims_conflict_with_ground_truth(
            {"repo_path": str(repo_path)},
            {
                "self_review_issues": [
                    {
                        "severity": "high",
                        "file": "src/x.jsx",
                        "message": "x.jsx does not exist; not implemented.",
                        "blocking": True,
                    }
                ]
            },
        )
        assert conflict is True
        assert "src/x.jsx" in reason

        # Model claims the changed-files evidence excludes the file even
        # though the file exists — also contradicts reality.
        conflict, reason = _self_assessment_claims_conflict_with_ground_truth(
            {"repo_path": str(repo_path)},
            {
                "self_review_issues": [
                    {
                        "severity": "high",
                        "file": "src/x.jsx",
                        "message": "changed files list does not include src/x.jsx, so the implementation cannot be verified.",
                        "blocking": True,
                    }
                ]
            },
        )
        assert conflict is True
        assert "src/x.jsx" in reason

        # Model says an existing file cannot be inspected — also a
        # contradiction once deterministic source evidence can read it.
        conflict, reason = _self_assessment_claims_conflict_with_ground_truth(
            {"repo_path": str(repo_path)},
            {
                "self_review_issues": [
                    {
                        "severity": "high",
                        "file": "src/x.jsx",
                        "message": "Unable to verify routing configuration because x.jsx is not inspectable.",
                        "blocking": True,
                    }
                ]
            },
        )
        assert conflict is True
        assert "src/x.jsx" in reason

        # Same data without a contradiction → no conflict.
        conflict, _ = _self_assessment_claims_conflict_with_ground_truth(
            {"repo_path": str(repo_path)},
            {
                "self_review_issues": [
                    {
                        "severity": "high",
                        "file": "src/x.jsx",
                        "message": "Variable naming is non-idiomatic.",
                        "blocking": True,
                    }
                ]
            },
        )
        assert conflict is False

        # No file claim → no conflict.
        conflict, _ = _self_assessment_claims_conflict_with_ground_truth(
            {"repo_path": str(repo_path)},
            {
                "self_review_issues": [
                    {
                        "severity": "high",
                        "message": "Generic implementation note (no file).",
                        "blocking": True,
                    }
                ]
            },
        )
        assert conflict is False

    async def test_try_ground_truth_re_prompt_recovers(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        impl_file = repo_path / "src" / "a.jsx"
        impl_file.parent.mkdir(parents=True, exist_ok=True)
        impl_file.write_text("x" * 200, encoding="utf-8")

        class _MockRuntime:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, prompt, **kw):
                self.calls += 1
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 0.95,
                            "verdict": "pass",
                            "criteria_checks": [],
                            "component_checks": [],
                            "self_review_issues": [],
                            "gaps": [],
                            "summary": "OK",
                        }
                    )
                }

        recovered, gt_data, reason = _try_ground_truth_re_prompt(
            state={
                "repo_path": str(repo_path),
                "changes_made": ["src/a.jsx"],
                "test_results": {"passed": 5, "failed": 0, "build_ok": True, "test_ok": True},
            },
            data={"verdict": "fail", "score": 0.5, "gaps": [], "self_review_issues": []},
            runtime=_MockRuntime(),
            prompt="original prompt",
            system_prompt="system",
            acceptance_criteria_count=0,
            log=MagicMock(),
        )
        assert recovered is True
        assert gt_data["verdict"] == "pass"
        assert reason == ""

    async def test_try_ground_truth_re_prompt_rejects_still_hallucinated(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        impl_file = repo_path / "src" / "a.jsx"
        impl_file.parent.mkdir(parents=True, exist_ok=True)
        impl_file.write_text("x" * 200, encoding="utf-8")

        class _MockRuntime:
            def run(self, prompt, **kw):
                # Re-prompt still claims the file is missing. The score
                # and verdict are consistent with the (still-claimed)
                # blocking issue so the validator passes — but the
                # ground-truth contradiction must still be caught.
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 0.5,
                            "verdict": "fail",
                            "criteria_checks": [],
                            "component_checks": [],
                            "self_review_issues": [
                                {
                                    "severity": "high",
                                    "file": "src/a.jsx",
                                    "message": "a.jsx is missing; not implemented.",
                                    "blocking": True,
                                }
                            ],
                            "gaps": [],
                            "summary": "OK",
                        }
                    )
                }

        recovered, gt_data, reason = _try_ground_truth_re_prompt(
            state={
                "repo_path": str(repo_path),
                "changes_made": ["src/a.jsx"],
                "test_results": {"passed": 5, "failed": 0, "build_ok": True, "test_ok": True},
            },
            data={"verdict": "fail", "score": 0.5, "gaps": [], "self_review_issues": []},
            runtime=_MockRuntime(),
            prompt="original prompt",
            system_prompt="system",
            acceptance_criteria_count=0,
            log=MagicMock(),
        )
        assert recovered is False
        assert gt_data is None
        assert "conflict" in reason.lower() or "src/a.jsx" in reason

    async def test_self_assess_fails_when_self_review_issues_exist(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        class _MockRuntime:
            def run(self, prompt, **kw):
                return {
                    "raw_response": json.dumps(
                        {
                            "score": 0.72,
                            "verdict": "fail",
                            "gaps": [],
                            "component_checks": [],
                            "criteria_checks": [],
                            "self_review_issues": [
                                {
                                    "severity": "high",
                                    "file": "src/app.py",
                                    "line": 27,
                                    "message": "Request payload is written without validation.",
                                    "suggestion": "Validate the payload before persisting it.",
                                    "blocking": True,
                                }
                            ],
                            "summary": "Found a merge-blocking issue.",
                        }
                    )
                }

        state = {
            "_runtime": _MockRuntime(),
            "repo_path": str(repo_path),
            "workspace_path": str(tmp_path),
            "definition_of_done": {"screenshot_required": False},
            "changes_made": ["src/app.py"],
            "implementation_summary": "Updated the request handler.",
            "test_results": {"passed": 4, "failed": 0},
        }

        result = await self_assess(state)

        assert result["route"] == "fail"
        assert result["self_assessment"]["verdict"] == "fail"
        assert "src/app.py:27 - Request payload is written without validation." in result["self_assessment"]["gaps"]

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
                            "severity": "high",
                            "blocking": True,
                            "file": "src/pages/PracticeQuizPage.jsx",
                            "message": "Blocking review issue.",
                        },
                        {
                            "severity": "medium",
                            "file": "src/pages/PracticeQuizPage.jsx",
                            "message": "Real actionable issue.",
                        },
                        {
                            "severity": "high",
                            "blocking": True,
                            "source_phase": "requirements",
                            "file": "src/pages/PracticeQuizPage.jsx",
                            "message": "Heading uses text-primary instead of text-on-surface per design spec typography color token guidance.",
                            "suggestion": "Switch the token to match the design spec.",
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
        assert "Blocking review issue." in runtime.prompt
        assert "Real actionable issue." not in runtime.prompt
        assert "text-primary instead of text-on-surface" not in runtime.prompt
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

    async def test_implement_changes_with_runtime(self, tmp_path, monkeypatch):
        from framework.runtime.adapter import AgenticCapabilities, AgenticResult
        from framework.validation_gates import ValidationResult

        monkeypatch.setattr(
            "framework.validation_gates.validate_files_changed",
            lambda repo_path: ValidationResult(True, "files_changed"),
        )

        class _MockRuntime:
            def __init__(self):
                self.kwargs = {}

            def agentic_capabilities(self):
                return AgenticCapabilities(
                    backend="connect-agent",
                    agentic=True,
                    constellation_tools=True,
                    allowed_tools=True,
                )

            def run_agentic(self, task, **kw):
                self.kwargs = kw
                kw["on_progress"]("managed turn 1/50")
                return AgenticResult(
                    success=True,
                    summary="Implemented login form in src/login.py",
                    tool_calls=[
                        {"tool": "write_file", "arguments": "src/login.py", "turn": 1},
                    ],
                    backend_used="mock",
                )

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "user_request": "Add login",
            "implementation_plan": "Create login form",
            "repo_path": str(tmp_path / "repo"),
            "workspace_path": str(tmp_path),
            "branch_name": "feature/login",
            "_allowed_tools": ["read_file", "write_file", "run_command"],
        }
        os.makedirs(state["repo_path"])
        result = await implement_changes(state)
        assert result["agentic_success"] is True
        assert "Implemented" in result["implementation_summary"]
        assert runtime.kwargs["tools"] == ["read_file", "write_file", "run_command"]
        assert runtime.kwargs["allowed_tools"] == ["read_file", "write_file", "run_command"]

    async def test_implement_changes_budgets_large_context_for_agentic_backends(
        self, tmp_path, monkeypatch
    ):
        from framework.runtime.adapter import AgenticCapabilities, AgenticResult
        from framework.validation_gates import ValidationResult

        monkeypatch.setattr(
            "framework.validation_gates.validate_files_changed",
            lambda repo_path: ValidationResult(True, "files_changed"),
        )

        class _MockRuntime:
            def __init__(self):
                self.task = ""

            def agentic_capabilities(self):
                return AgenticCapabilities(
                    backend="copilot-cli",
                    agentic=True,
                    constellation_tools=True,
                    allowed_tools=True,
                    cwd=True,
                )

            def run_agentic(self, task, **kw):
                self.task = task
                return AgenticResult(success=True, summary="Implemented", backend_used="copilot-cli")

        workspace_path = tmp_path / "workspace"
        repo_path = workspace_path / "repo"
        design_dir = workspace_path / "ui-design" / "stitch"
        repo_path.mkdir(parents=True)
        design_dir.mkdir(parents=True)
        (design_dir / "code.html").write_text("HTML_START\n" + ("DESIGN_HTML_BULK\n" * 2000), encoding="utf-8")
        (design_dir / "DESIGN.md").write_text("SPEC_START\n" + ("DESIGN_SPEC_BULK\n" * 2000), encoding="utf-8")

        runtime = _MockRuntime()
        await implement_changes(
            {
                "_runtime": runtime,
                "user_request": "Implement feature",
                "implementation_plan": "<think>hidden</think>\n" + ("PLAN_BULK\n" * 2000),
                "jira_context": {
                    "key": "ABC-1",
                    "fields": {
                        "summary": "Implement feature",
                        "description": "JIRA_DESCRIPTION\n" + ("JIRA_BULK\n" * 2000),
                    },
                },
                "design_context": {"payload": "DESIGN_CONTEXT_BULK" * 2000},
                "skill_context": "SKILL_BULK\n" * 2000,
                "memory_context": "MEMORY_BULK\n" * 2000,
                "repo_path": str(repo_path),
                "workspace_path": str(workspace_path),
                "branch_name": "feature/example",
                "_allowed_tools": ["read_file", "write_file", "run_command"],
            }
        )

        assert "hidden" not in runtime.task
        assert runtime.task.count("PLAN_BULK") < 500
        assert runtime.task.count("JIRA_BULK") < 600
        assert runtime.task.count("DESIGN_HTML_BULK") < 900
        assert runtime.task.count("DESIGN_SPEC_BULK") < 500
        assert "full source remains available" in runtime.task

    async def test_implement_changes_continues_when_protocol_fails_but_files_changed(
        self, tmp_path, monkeypatch
    ):
        from framework.runtime.adapter import AgenticCapabilities, AgenticResult
        from framework.validation_gates import ValidationResult

        monkeypatch.setattr(
            "framework.validation_gates.validate_files_changed",
            lambda repo_path: ValidationResult(True, "files_changed"),
        )
        monkeypatch.setattr(
            "agents.web_dev.nodes._git_branch_changed_files",
            lambda *args, **kwargs: [],
        )
        worktree_calls = iter([[], ["src/App.tsx"]])
        monkeypatch.setattr(
            "agents.web_dev.nodes._git_worktree_changed_files",
            lambda *args, **kwargs: next(worktree_calls, ["src/App.tsx"]),
        )

        class _MockRuntime:
            def agentic_capabilities(self):
                return AgenticCapabilities(
                    backend="copilot-cli",
                    agentic=True,
                    constellation_tools=True,
                    allowed_tools=True,
                    cwd=True,
                )

            def run_agentic(self, task, **kw):
                return AgenticResult(
                    success=False,
                    summary=(
                        "copilot-cli managed agentic loop did not return valid "
                        "managed-loop JSON after 50 turns."
                    ),
                    backend_used="copilot-cli",
                )

        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = await implement_changes(
            {
                "_runtime": _MockRuntime(),
                "user_request": "Implement feature",
                "implementation_plan": "Create page",
                "repo_path": str(repo_path),
                "workspace_path": str(tmp_path),
                "branch_name": "feature/example",
                "_allowed_tools": ["read_file", "write_file"],
            }
        )

        assert result["agentic_success"] is False
        assert "Partial implementation" in result["implementation_summary"]
        assert result["changes_made"] == ["src/App.tsx"]

    async def test_implement_changes_error_names_actual_backend(self, monkeypatch, tmp_path):
        from framework.runtime.adapter import AgenticResult

        class _MockRuntime:
            def run_agentic(self, task, **kw):
                return AgenticResult(
                    success=False,
                    summary="copilot-cli error: [Errno 7] Argument list too long: 'copilot'",
                    backend_used="copilot-cli",
                )

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        monkeypatch.setattr("agents.web_dev.nodes._git_branch_changed_files", lambda *args, **kwargs: [])
        monkeypatch.setattr("agents.web_dev.nodes._git_worktree_changed_files", lambda *args, **kwargs: [])

        with pytest.raises(RuntimeError) as exc_info:
            await implement_changes({
                "_runtime": _MockRuntime(),
                "user_request": "Implement feature",
                "implementation_plan": "Create page",
                "repo_path": str(repo_path),
                "branch_name": "feature/example",
            })

        message = str(exc_info.value)
        assert "copilot-cli returned error" in message
        assert "claude-code returned error" not in message

    async def test_implement_changes_rejects_frontend_config_only_progress(
        self, monkeypatch, tmp_path
    ):
        from framework.runtime.adapter import AgenticCapabilities, AgenticResult

        monkeypatch.setattr(
            "agents.web_dev.nodes._bootstrap_minimal_frontend_scaffold",
            lambda *args, **kwargs: [],
        )

        class _MockRuntime:
            def agentic_capabilities(self):
                return AgenticCapabilities(
                    backend="copilot-cli",
                    agentic=True,
                    constellation_tools=True,
                    allowed_tools=True,
                    cwd=True,
                )

            def run_agentic(self, task, **kw):
                return AgenticResult(success=True, summary="Updated package.json", backend_used="copilot-cli")

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "package.json").write_text('{"dependencies":{"react":"^18.2.0"}}', encoding="utf-8")

        with pytest.raises(RuntimeError) as exc_info:
            await implement_changes(
                {
                    "_runtime": _MockRuntime(),
                    "user_request": "Implement a React page",
                    "implementation_plan": "Build the UI",
                    "repo_path": str(repo_path),
                    "workspace_path": str(tmp_path),
                    "branch_name": "feature/frontend",
                    "tech_stack": ["react", "vite"],
                    "_allowed_tools": ["read_file", "write_file", "run_command"],
                }
            )

        assert "did not create a frontend source entrypoint" in str(exc_info.value)

    async def test_implement_changes_uses_configurable_agentic_budget(
        self, monkeypatch, tmp_path
    ):
        from framework.runtime.adapter import AgenticCapabilities, AgenticResult
        from framework.validation_gates import ValidationResult

        monkeypatch.setenv("WEB_DEV_IMPLEMENT_MAX_TURNS", "96")
        monkeypatch.setenv("WEB_DEV_IMPLEMENT_TIMEOUT_SECONDS", "3600")
        monkeypatch.setattr(
            "framework.validation_gates.validate_files_changed",
            lambda repo_path: ValidationResult(True, "files_changed"),
        )

        class _MockRuntime:
            def __init__(self):
                self.kwargs = {}

            def agentic_capabilities(self):
                return AgenticCapabilities(
                    backend="copilot-cli",
                    agentic=True,
                    constellation_tools=True,
                    allowed_tools=True,
                    cwd=True,
                )

            def run_agentic(self, task, **kw):
                self.kwargs = kw
                return AgenticResult(success=True, summary="Implemented backend change", backend_used="mock")

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        runtime = _MockRuntime()

        await implement_changes(
            {
                "_runtime": runtime,
                "user_request": "Implement a backend change",
                "implementation_plan": "Update the service.",
                "repo_path": str(repo_path),
                "workspace_path": str(tmp_path),
                "branch_name": "feature/backend",
                "tech_stack": ["python"],
                "_allowed_tools": ["read_file", "write_file", "run_command"],
            }
        )

        assert runtime.kwargs["max_turns"] == 96
        assert runtime.kwargs["timeout"] == 3600

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

    async def test_fix_tests_with_runtime(self, tmp_path):
        from framework.runtime.adapter import AgenticCapabilities, AgenticResult

        class _MockRuntime:
            def __init__(self):
                self.kwargs = {}

            def agentic_capabilities(self):
                return AgenticCapabilities(
                    backend="connect-agent",
                    agentic=True,
                    constellation_tools=True,
                    allowed_tools=True,
                )

            def run_agentic(self, task, **kw):
                self.kwargs = kw
                return AgenticResult(success=True, summary="Fixed null check in login.py", backend_used="mock")

        runtime = _MockRuntime()
        state = {
            "_runtime": runtime,
            "test_output": "AssertionError: None is not True",
            "repo_path": str(tmp_path / "repo"),
            "workspace_path": str(tmp_path),
            "changes_made": ["src/login.py"],
            "_allowed_tools": ["read_file", "run_command"],
        }
        os.makedirs(state["repo_path"])
        result = await fix_tests(state)
        assert result["fix_attempted"] is True
        assert "Fixed" in result["fix_summary"]
        assert runtime.kwargs["tools"] == ["read_file", "run_command"]
        assert runtime.kwargs["allowed_tools"] == ["read_file", "run_command"]
        assert runtime.kwargs["max_turns"] == 35
        assert runtime.kwargs["timeout"] == 900

    async def test_fix_tests_accepts_recoverable_protocol_failure_with_edits(
        self, tmp_path
    ):
        from framework.runtime.adapter import AgenticCapabilities, AgenticResult

        class _MockRuntime:
            def agentic_capabilities(self):
                return AgenticCapabilities(
                    backend="copilot-cli",
                    agentic=True,
                    constellation_tools=True,
                    allowed_tools=True,
                    cwd=True,
                )

            def run_agentic(self, task, **kw):
                return AgenticResult(
                    success=False,
                    summary=(
                        "copilot-cli managed agentic loop did not return valid "
                        "managed-loop JSON after 35 turns."
                    ),
                    backend_used="copilot-cli",
                    tool_calls=[{"tool": "edit_file", "arguments": {"path": "src/App.test.tsx"}}],
                )

        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = await fix_tests(
            {
                "_runtime": _MockRuntime(),
                "test_output": "failing test",
                "repo_path": str(repo_path),
                "workspace_path": str(tmp_path),
                "changes_made": ["src/App.test.tsx"],
                "_allowed_tools": ["read_file", "edit_file", "run_command"],
            }
        )

        assert result["agentic_success"] is True
        assert "Partial test-fix progress" in result["fix_summary"]

    async def test_fix_gaps_with_runtime_uses_same_agentic_policy(self, tmp_path):
        from framework.runtime.adapter import AgenticCapabilities, AgenticResult

        class _MockRuntime:
            def __init__(self):
                self.kwargs = {}

            def agentic_capabilities(self):
                return AgenticCapabilities(
                    backend="connect-agent",
                    agentic=True,
                    constellation_tools=True,
                    allowed_tools=True,
                )

            def run_agentic(self, task, **kw):
                self.kwargs = kw
                return AgenticResult(success=True, summary="Fixed self-check gap", backend_used="mock")

        runtime = _MockRuntime()
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = await fix_gaps(
            {
                "_runtime": runtime,
                "repo_path": str(repo_path),
                "workspace_path": str(tmp_path),
                "changes_made": ["src/login.py"],
                "self_assessment": {"gaps": ["Address a generic implementation gap."]},
                "_allowed_tools": ["read_file", "write_file", "run_command"],
            }
        )

        assert result["fix_gaps_attempted"] is True
        assert runtime.kwargs["tools"] == ["read_file", "write_file", "run_command"]
        assert runtime.kwargs["allowed_tools"] == ["read_file", "write_file", "run_command"]
        assert runtime.kwargs["max_turns"] == 30
        assert runtime.kwargs["timeout"] == 600

    async def test_fix_gaps_accepts_recoverable_protocol_failure_with_edits(
        self, tmp_path
    ):
        from framework.runtime.adapter import AgenticCapabilities, AgenticResult

        class _MockRuntime:
            def agentic_capabilities(self):
                return AgenticCapabilities(
                    backend="copilot-cli",
                    agentic=True,
                    constellation_tools=True,
                    allowed_tools=True,
                    cwd=True,
                )

            def run_agentic(self, task, **kw):
                return AgenticResult(
                    success=False,
                    summary=(
                        "copilot-cli managed agentic loop did not return valid "
                        "managed-loop JSON after 30 turns."
                    ),
                    backend_used="copilot-cli",
                    tool_calls=[{"tool": "write_file", "arguments": {"path": "src/App.tsx"}}],
                )

        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = await fix_gaps(
            {
                "_runtime": _MockRuntime(),
                "repo_path": str(repo_path),
                "workspace_path": str(tmp_path),
                "changes_made": ["src/App.tsx"],
                "self_assessment": {"gaps": ["Address a generic implementation gap."]},
                "_allowed_tools": ["read_file", "write_file", "run_command"],
            }
        )

        assert result["agentic_success"] is True
        assert "Partial self-check-gap-fix progress" in result["fix_gaps_summary"]

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

    async def test_create_pr_revision_mode_raises_when_push_fails(self):
        from unittest.mock import patch

        state = {
            "_runtime": object(),
            "revision_mode": True,
            "existing_pr_url": "https://github.com/org/repo/pull/42",
            "existing_pr_number": 42,
            "branch_name": "feature/login",
            "repo_path": "/tmp/repo",
            "repo_url": "https://github.com/org/repo",
            "jira_context": {"key": "ABC-123"},
            "changes_made": ["src/login.py"],
            "revision_feedback": "Address review comments",
        }

        def _boundary(_state, tool_name, _args):
            if tool_name == "scm_push":
                return {"error": "Push failed", "detail": "stale info"}
            raise AssertionError(f"unexpected boundary tool after push failure: {tool_name}")

        with patch("agents.web_dev.nodes._check_pr_status_conflict", return_value={"conflict": False}), patch(
            "agents.web_dev.nodes._git_commit_all_pending",
            return_value=["src/login.py"],
        ), patch(
            "agents.web_dev.nodes._git_branch_changed_files",
            return_value=["src/login.py"],
        ), patch(
            "agents.web_dev.nodes._call_boundary_tool",
            side_effect=_boundary,
        ):
            with pytest.raises(RuntimeError, match="Revision push failed: Push failed"):
                await create_pr(state)

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

    def test_run_command_rejects_command_outside_permission_patterns(self, tmp_path):
        from agents.web_dev.coding_tools import RunCommandTool
        from framework.audit_log import (
            clear_permission_audit_context,
            set_permission_audit_context,
        )
        from framework.permissions import PermissionEngine, PermissionSet
        from framework.tools.registry import get_registry

        registry = get_registry()
        registry.set_permission_engine(
            PermissionEngine(
                PermissionSet(
                    allowed_tools=["run_command"],
                    custom={"allowed_command_patterns": [r"^python -m pytest(\s|$).*"]},
                )
            )
        )
        set_permission_audit_context(
            workspace_path=str(tmp_path),
            agent_id="web-dev",
            task_id="task-command-audit",
        )
        try:
            result = RunCommandTool().execute_sync(
                command="python -c 'print(1)'",
                cwd=str(tmp_path),
            )
        finally:
            clear_permission_audit_context()
            registry.set_permission_engine(None)

        assert result.error is not None
        assert "not permitted" in result.error
        audit_path = tmp_path / "web-dev" / "permission-denials.jsonl"
        records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        assert records[-1]["operation"] == "command"
        assert records[-1]["command"] == "python -c 'print(1)'"
        assert records[-1]["status"] == "denied"

    def test_run_command_normalizes_cd_prefix_before_permission_check(self, tmp_path, monkeypatch):
        from agents.web_dev.coding_tools import RunCommandTool
        from framework.audit_log import (
            clear_permission_audit_context,
            set_permission_audit_context,
        )
        from framework.permissions import PermissionEngine, PermissionSet
        from framework.tools.registry import get_registry

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        calls = []

        class _Proc:
            returncode = 0
            stdout = "installed\n"
            stderr = ""

        def _fake_run(args, **kwargs):
            calls.append({"args": args, "cwd": kwargs.get("cwd")})
            return _Proc()

        monkeypatch.setattr("agents.web_dev.coding_tools.subprocess.run", _fake_run)
        registry = get_registry()
        registry.set_permission_engine(
            PermissionEngine(
                PermissionSet(
                    allowed_tools=["run_command"],
                    custom={"allowed_command_patterns": [r"^npm install(\s|$).*"]},
                )
            )
        )
        set_permission_audit_context(
            workspace_path=str(tmp_path),
            agent_id="web-dev",
            task_id="task-command-audit",
        )
        try:
            result = RunCommandTool().execute_sync(
                command=f"cd {repo_path} && npm install",
                cwd=str(tmp_path),
            )
        finally:
            clear_permission_audit_context()
            registry.set_permission_engine(None)

        assert not result.error
        assert json.loads(result.output)["success"] is True
        assert calls == [{"args": ["npm", "install"], "cwd": str(repo_path)}]
        assert not (tmp_path / "web-dev" / "permission-denials.jsonl").exists()

    def test_run_command_standalone_cd_is_guidance_not_permission_denial(self, tmp_path):
        from agents.web_dev.coding_tools import RunCommandTool
        from framework.audit_log import (
            clear_permission_audit_context,
            set_permission_audit_context,
        )
        from framework.permissions import PermissionEngine, PermissionSet
        from framework.tools.registry import get_registry

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        registry = get_registry()
        registry.set_permission_engine(
            PermissionEngine(
                PermissionSet(
                    allowed_tools=["run_command"],
                    custom={"allowed_command_patterns": [r"^npm install(\s|$).*"]},
                )
            )
        )
        set_permission_audit_context(
            workspace_path=str(tmp_path),
            agent_id="web-dev",
            task_id="task-command-audit",
        )
        try:
            result = RunCommandTool().execute_sync(command=f"cd {repo_path}")
        finally:
            clear_permission_audit_context()
            registry.set_permission_engine(None)

        assert result.error is not None
        assert "cwd" in result.error
        assert "not persist" in result.error
        assert not (tmp_path / "web-dev" / "permission-denials.jsonl").exists()

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
