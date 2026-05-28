"""Tests for Code Review Agent workflow."""
import json
import pytest
from unittest.mock import MagicMock
from framework.agent import AgentServices
from framework.task_store import InMemoryTaskStore
from framework.workflow import START, END
from agents.code_review.agent import CodeReviewAgent, code_review_workflow, code_review_definition
from agents.code_review.nodes import (
    load_pr_context,
    review_quality,
    review_security,
    review_tests,
    review_requirements,
    generate_report,
    _parse_issue_list,
)


# ---------------------------------------------------------------------------
# Shared mock runtime
# ---------------------------------------------------------------------------

def _make_runtime(response: str):
    """Return a mock runtime whose run() yields *response* as raw_response."""
    class _MockRuntime:
        def run(self, prompt, **kw):
            return {"raw_response": response}
    return _MockRuntime()


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


# ---------------------------------------------------------------------------
# Compile tests
# ---------------------------------------------------------------------------

class TestCodeReviewWorkflowCompile:

    def test_code_review_workflow_compiles(self):
        compiled = code_review_workflow.compile()
        assert compiled.name == "code_review"

    def test_code_review_workflow_has_all_nodes(self):
        compiled = code_review_workflow.compile()
        expected_nodes = {
            "load_pr_context", "review_quality", "review_security",
            "review_tests", "review_requirements", "review_ui_design", "generate_report",
        }
        assert expected_nodes == set(compiled.nodes.keys())

    def test_code_review_definition_fields(self):
        from framework.agent import AgentMode, ExecutionMode
        assert code_review_definition.agent_id == "code-review"
        assert code_review_definition.mode == AgentMode.TASK
        assert code_review_definition.execution_mode == ExecutionMode.PER_TASK
        assert code_review_definition.permissions.get("scm") == "read"


class TestCodeReviewExecutionContract:

    async def test_handle_message_fails_closed_without_execution_contract(self):
        agent = CodeReviewAgent(definition=code_review_definition, services=_agent_services())

        result = await agent.handle_message({"message": {"parts": [{"text": "Review PR"}], "metadata": {}}})

        assert result["task"]["status"]["state"] == "TASK_STATE_FAILED"
        assert "Missing executionContract" in result["task"]["status"]["message"]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# _parse_issue_list helper
# ---------------------------------------------------------------------------

class TestParseIssueList:

    def test_valid_json_array(self):
        issues = _parse_issue_list('[{"severity":"high","file":"a.py","line":10,"message":"x","suggestion":"y"}]')
        assert len(issues) == 1
        assert issues[0]["severity"] == "high"

    def test_empty_array(self):
        assert _parse_issue_list("[]") == []

    def test_array_embedded_in_text(self):
        text = 'Here are issues: [{"severity":"low","message":"z"}]'
        issues = _parse_issue_list(text)
        assert len(issues) == 1

    def test_invalid_returns_empty(self):
        assert _parse_issue_list("not json at all") == []

    def test_object_not_array_returns_empty(self):
        assert _parse_issue_list('{"error": "bad"}') == []


# ---------------------------------------------------------------------------
# Node tests
# ---------------------------------------------------------------------------

class TestLoadPrContext:

    async def test_loads_from_state_directly(self):
        state = {
            "pr_diff": "diff --git a/x.py...",
            "changed_files": ["x.py"],
            "pr_description": "Fix bug",
            "commit_messages": ["fix: null check"],
        }
        result = await load_pr_context(state)
        assert result["pr_diff"] == "diff --git a/x.py..."
        assert result["changed_files"] == ["x.py"]

    async def test_loads_from_metadata(self):
        state = {
            "metadata": {
                "prDiff": "diff --git a/y.py...",
                "changedFiles": ["y.py"],
                "prDescription": "Add feature",
            }
        }
        result = await load_pr_context(state)
        assert result["pr_diff"] == "diff --git a/y.py..."
        assert result["changed_files"] == ["y.py"]

    async def test_empty_state(self):
        result = await load_pr_context({})
        assert result["pr_diff"] == ""
        assert result["changed_files"] == []
        assert result["pr_description"] == ""

    async def test_writes_review_start_checkpoint(self, tmp_path):
        state = {
            "metadata": {
                "prUrl": "https://example.com/pr/1",
                "workspacePath": str(tmp_path),
                "contextManifestPath": "team-lead/context-manifest.json",
                "jiraContext": {"key": "PROJ-123"},
            }
        }

        await load_pr_context(state)

        checkpoint_file = tmp_path / "code-review" / "review-checkpoints" / "review-start.json"
        assert checkpoint_file.exists()

    async def test_loads_workspace_boundary_artifacts_and_latest_self_assessment(self, tmp_path):
        workspace_path = tmp_path / "task-123"
        jira_dir = workspace_path / "jira" / "PROJ-123"
        jira_dir.mkdir(parents=True)
        (jira_dir / "ticket.json").write_text(json.dumps({
            "data": {
                "key": "PROJ-123",
                "fields": {"description": "Ship the lesson page"},
            }
        }))

        design_dir = workspace_path / "ui-design" / "stitch"
        design_dir.mkdir(parents=True)
        (design_dir / "DESIGN.md").write_text("# Design spec")
        (design_dir / "code.html").write_text("<main>lesson</main>")
        (design_dir / "screen-meta.json").write_text(json.dumps({"screen": {"title": "Lesson Library"}}))

        web_dev_dir = workspace_path / "web-dev"
        web_dev_dir.mkdir(parents=True)
        (web_dev_dir / "pr-evidence.json").write_text(json.dumps({
            "data": {
                "pr_url": "https://github.com/org/repo/pull/12",
                "pr_number": 12,
                "changed_files": ["src/App.tsx"],
            }
        }))
        (web_dev_dir / "self-assessment-1.json").write_text(json.dumps({"data": {"score": 0.9}}))
        (web_dev_dir / "self-assessment-2.json").write_text(json.dumps({"data": {"score": 1.0}}))

        state = {
            "_task_id": "task-123",
            "metadata": {
                "repoUrl": "https://github.com/org/repo",
                "workspacePath": str(workspace_path),
                "contextManifestPath": "team-lead/context-manifest.json",
                "jiraContext": {"key": "PROJ-123"},
            },
        }
        (workspace_path / "team-lead").mkdir(parents=True)
        (workspace_path / "team-lead" / "context-manifest.json").write_text("{}")

        result = await load_pr_context(state)

        assert result["pr_number"] == 12
        assert result["changed_files"] == ["src/App.tsx"]
        assert result["jira_context"]["key"] == "PROJ-123"
        assert result["design_context"]["spec_markdown"] == "# Design spec"
        assert "jira/PROJ-123/ticket.json" in result["checked_artifacts"]
        assert "ui-design/stitch/DESIGN.md" in result["checked_artifacts"]
        assert "web-dev/pr-evidence.json" in result["checked_artifacts"]
        assert "web-dev/self-assessment-2.json" in result["checked_artifacts"]
        assert all(not path.startswith("code-review/") for path in result["checked_artifacts"])
        assert (workspace_path / "code-review" / "review-checkpoints" / "review-start.json").exists()

    async def test_forwards_parent_supplied_child_permissions_to_scm_fallbacks(self, monkeypatch):
        captured: list[dict] = []

        class StubRegistry:
            def execute_sync(self, name, arguments):
                captured.append({"name": name, "arguments": arguments})
                if name == "scm_get_pr_info":
                    return json.dumps({"description": "Review this PR", "commits": []})
                if name == "scm_get_pr_diff":
                    return json.dumps({
                        "diff_text": "diff --git a/src/App.tsx b/src/App.tsx",
                        "changed_files": [{"filename": "src/App.tsx"}],
                    })
                raise AssertionError(f"unexpected tool {name}")

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        permissions = {
            "allowedTools": ["scm_get_pr_info", "scm_get_pr_diff"],
            "deniedTools": [],
            "scm": "read",
            "filesystem": "workspace-only",
            "custom": {},
        }
        result = await load_pr_context(
            {
                "_task_id": "task-123",
                "metadata": {
                    "prUrl": "https://github.com/org/repo/pull/12",
                    "repoUrl": "https://github.com/org/repo",
                    "prNumber": 12,
                    "permissions": permissions,
                },
            }
        )

        assert result["pr_diff"].startswith("diff --git")
        assert [entry["name"] for entry in captured] == ["scm_get_pr_info", "scm_get_pr_diff"]
        assert captured[0]["arguments"]["permissions"] == permissions
        assert captured[1]["arguments"]["permissions"] == permissions

    async def test_load_pr_context_writes_agent_log(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

        await load_pr_context({"_task_id": "task-123"})

        assert (tmp_path / "task-123" / "code-review" / "agent.log").exists()


class TestReviewQuality:

    async def test_no_runtime_returns_empty(self):
        result = await review_quality({})
        assert result["quality_issues"] == []

    async def test_no_diff_returns_empty(self):
        result = await review_quality({"_runtime": _make_runtime("[]")})
        assert result["quality_issues"] == []

    async def test_with_runtime_and_diff(self):
        issues_json = '[{"severity":"medium","file":"a.py","line":5,"message":"Magic number","suggestion":"Use constant"}]'
        state = {
            "_runtime": _make_runtime(issues_json),
            "pr_diff": "- x = 42",
            "changed_files": ["a.py"],
            "pr_description": "",
        }
        result = await review_quality(state)
        assert len(result["quality_issues"]) == 1
        assert result["quality_issues"][0]["severity"] == "medium"

    async def test_with_runtime_no_issues(self):
        state = {
            "_runtime": _make_runtime("[]"),
            "pr_diff": "+ pass",
        }
        result = await review_quality(state)
        assert result["quality_issues"] == []


class TestReviewSecurity:

    async def test_no_runtime_returns_empty(self):
        result = await review_security({})
        assert result["security_issues"] == []

    async def test_no_diff_returns_empty(self):
        result = await review_security({"_runtime": _make_runtime("[]")})
        assert result["security_issues"] == []

    async def test_critical_sql_injection(self):
        issues_json = '[{"severity":"critical","file":"db.py","line":20,"message":"SQL injection risk","owasp":"A03:2021 Injection","suggestion":"Use parameterised queries"}]'
        state = {
            "_runtime": _make_runtime(issues_json),
            "pr_diff": '+ query = f"SELECT * FROM users WHERE id = {user_id}"',
        }
        result = await review_security(state)
        assert result["security_issues"][0]["severity"] == "critical"


class TestReviewTests:

    async def test_no_runtime_returns_empty(self):
        result = await review_tests({})
        assert result["test_issues"] == []

    async def test_no_diff_returns_empty(self):
        result = await review_tests({"_runtime": _make_runtime("[]")})
        assert result["test_issues"] == []

    async def test_missing_coverage(self):
        issues_json = '[{"severity":"high","file":"login.py","line":null,"message":"No tests for login()","suggestion":"Add unit tests"}]'
        state = {
            "_runtime": _make_runtime(issues_json),
            "pr_diff": "+ def login(user, pw): ...",
        }
        result = await review_tests(state)
        assert result["test_issues"][0]["severity"] == "high"


class TestReviewRequirements:

    async def test_no_runtime_returns_empty(self):
        result = await review_requirements({})
        assert result["requirement_gaps"] == []

    async def test_no_diff_returns_empty(self):
        result = await review_requirements({"_runtime": _make_runtime("[]"), "original_requirements": "Must have login"})
        assert result["requirement_gaps"] == []

    async def test_no_requirements_returns_empty(self):
        """When no requirements are provided, skip LLM call."""
        state = {"_runtime": _make_runtime("[]"), "pr_diff": "+code"}
        result = await review_requirements(state)
        assert result["requirement_gaps"] == []

    async def test_requirement_gap_found(self):
        issues_json = '[{"severity":"high","requirement":"AC-3: password must be hashed","message":"Password stored plaintext","suggestion":"Use bcrypt"}]'
        state = {
            "_runtime": _make_runtime(issues_json),
            "pr_diff": '+ self.password = password',
            "original_requirements": "AC-3: passwords must be hashed with bcrypt",
        }
        result = await review_requirements(state)
        assert len(result["requirement_gaps"]) == 1


class TestGenerateReport:

    async def test_no_issues_approved(self):
        state = {
            "quality_issues": [],
            "security_issues": [],
            "test_issues": [],
            "requirement_gaps": [],
        }
        result = await generate_report(state)
        assert result["verdict"] == "approved"
        assert result["all_comments"] == []

    async def test_critical_issue_rejected(self):
        state = {
            "quality_issues": [{"severity": "critical", "message": "SQL injection"}],
            "security_issues": [],
            "test_issues": [],
            "requirement_gaps": [],
        }
        result = await generate_report(state)
        assert result["verdict"] == "rejected"
        assert result["severity_levels"]["critical"] == 1

    async def test_high_issue_rejected(self):
        state = {
            "quality_issues": [],
            "security_issues": [{"severity": "high", "message": "XSS"}],
            "test_issues": [],
            "requirement_gaps": [],
        }
        result = await generate_report(state)
        assert result["verdict"] == "rejected"

    async def test_medium_only_approved(self):
        state = {
            "quality_issues": [{"severity": "medium", "message": "Magic number"}],
            "security_issues": [],
            "test_issues": [],
            "requirement_gaps": [],
        }
        result = await generate_report(state)
        assert result["verdict"] == "approved"

    async def test_severity_counts(self):
        state = {
            "quality_issues": [
                {"severity": "critical", "message": "c1"},
                {"severity": "high", "message": "h1"},
                {"severity": "medium", "message": "m1"},
            ],
            "security_issues": [{"severity": "low", "message": "l1"}],
            "test_issues": [],
            "requirement_gaps": [],
        }
        result = await generate_report(state)
        levels = result["severity_levels"]
        assert levels["critical"] == 1
        assert levels["high"] == 1
        assert levels["medium"] == 1
        assert levels["low"] == 1

    async def test_report_summary_contains_verdict(self):
        state = {
            "quality_issues": [],
            "security_issues": [],
            "test_issues": [],
            "requirement_gaps": [],
        }
        result = await generate_report(state)
        assert "approved" in result["report_summary"].lower()

    async def test_writes_review_summary_checkpoint_and_checked_artifacts(self, tmp_path):
        state = {
            "quality_issues": [],
            "security_issues": [],
            "test_issues": [],
            "requirement_gaps": [],
            "workspace_path": str(tmp_path),
            "checked_artifacts": [
                "jira/PROJ-123/ticket.json",
                "ui-design/stitch/DESIGN.md",
                "web-dev/self-assessment-2.json",
                "web-dev/pr-evidence.json",
            ],
        }

        result = await generate_report(state)

        checkpoint_file = tmp_path / "code-review" / "review-checkpoints" / "review-summary.json"
        assert checkpoint_file.exists()
        assert "jira/PROJ-123/ticket.json" in result["checked_artifacts"]
        assert "ui-design/stitch/DESIGN.md" in result["checked_artifacts"]


class TestCodeReviewWorkflowExecution:

    async def test_full_review_no_issues(self):
        compiled = code_review_workflow.compile()
        state = {
            "pr_url": "https://github.com/test/pr/1",
            "repo_url": "https://github.com/test",
        }
        result = await compiled.invoke(state)
        assert result["verdict"] == "approved"

    async def test_full_review_with_runtime_issues(self):
        """End-to-end with mocked runtime returning issues."""
        compiled = code_review_workflow.compile()

        call_count = {"n": 0}
        responses = [
            '[{"severity":"high","file":"a.py","line":1,"message":"Bad naming","suggestion":"Rename"}]',
            "[]",  # security: no issues
            "[]",  # tests: no issues
            # requirements won't be called (no original_requirements)
        ]

        class _MultiRuntime:
            def run(self, prompt, **kw):
                idx = call_count["n"]
                call_count["n"] += 1
                resp = responses[idx] if idx < len(responses) else "[]"
                return {"raw_response": resp}

        state = {
            "_runtime": _MultiRuntime(),
            "pr_diff": "- old\n+ new",
            "changed_files": ["a.py"],
            "pr_description": "Refactor",
        }
        result = await compiled.invoke(state)
        # high severity → rejected
        assert result["verdict"] == "rejected"

    async def test_full_review_pipeline_keys(self):
        compiled = code_review_workflow.compile()
        state = {}
        result = await compiled.invoke(state)
        for key in ("pr_diff", "quality_issues", "security_issues", "test_issues", "requirement_gaps", "verdict"):
            assert key in result, f"Expected key '{key}' in result"

