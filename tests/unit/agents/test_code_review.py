"""Tests for Code Review Agent workflow."""
import pytest
from framework.workflow import START, END
from agents.code_review.agent import code_review_workflow, code_review_definition
from agents.code_review.nodes import (
    load_pr_context,
    review_quality,
    review_security,
    review_tests,
    review_requirements,
    generate_report,
)


class TestCodeReviewWorkflowCompile:

    def test_code_review_workflow_compiles(self):
        compiled = code_review_workflow.compile()
        assert compiled.name == "code_review"

    def test_code_review_workflow_has_all_nodes(self):
        compiled = code_review_workflow.compile()
        expected_nodes = {
            "load_pr_context", "review_quality", "review_security",
            "review_tests", "review_requirements", "generate_report",
        }
        assert expected_nodes == set(compiled.nodes.keys())

    def test_code_review_definition_fields(self):
        from framework.agent import AgentMode, ExecutionMode
        assert code_review_definition.agent_id == "code-review"
        assert code_review_definition.mode == AgentMode.TASK
        assert code_review_definition.execution_mode == ExecutionMode.PER_TASK


class TestCodeReviewNodes:

    async def test_load_pr_context(self):
        state = {}
        result = await load_pr_context(state)
        assert "pr_diff" in result
        assert "changed_files" in result

    async def test_review_quality_no_runtime(self):
        state = {}
        result = await review_quality(state)
        assert result["quality_issues"] == []

    async def test_review_security_no_runtime(self):
        state = {}
        result = await review_security(state)
        assert result["security_issues"] == []

    async def test_review_tests_no_runtime(self):
        state = {}
        result = await review_tests(state)
        assert result["test_issues"] == []

    async def test_review_requirements_no_runtime(self):
        state = {}
        result = await review_requirements(state)
        assert result["requirement_gaps"] == []

    async def test_generate_report_no_issues(self):
        state = {
            "quality_issues": [],
            "security_issues": [],
            "test_issues": [],
            "requirement_gaps": [],
        }
        result = await generate_report(state)
        assert result["verdict"] == "approved"
        assert result["all_comments"] == []

    async def test_generate_report_with_critical_issues(self):
        state = {
            "quality_issues": [{"severity": "critical", "message": "SQL injection"}],
            "security_issues": [],
            "test_issues": [],
            "requirement_gaps": [],
        }
        result = await generate_report(state)
        assert result["verdict"] == "rejected"
        assert result["severity_levels"]["critical"] == 1


class TestCodeReviewWorkflowExecution:

    async def test_full_review_no_issues(self):
        compiled = code_review_workflow.compile()
        state = {
            "pr_url": "https://github.com/test/pr/1",
            "repo_url": "https://github.com/test",
        }
        result = await compiled.invoke(state)
        assert result["verdict"] == "approved"

    async def test_full_review_pipeline(self):
        """Verify the review pipeline runs all phases in order."""
        compiled = code_review_workflow.compile()
        state = {}
        result = await compiled.invoke(state)
        # All phase outputs should be present
        assert "pr_diff" in result
        assert "quality_issues" in result
        assert "security_issues" in result
        assert "test_issues" in result
        assert "requirement_gaps" in result
        assert "verdict" in result
