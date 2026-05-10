"""Tests for Compass Agent workflow."""
import pytest
from framework.workflow import START, END
from agents.compass.agent import compass_workflow, compass_definition
from agents.compass.nodes import (
    classify_task,
    check_permissions,
    dispatch_task,
    wait_for_result,
    completeness_gate,
    summarize_for_user,
    handle_office_task,
)


class TestCompassWorkflowCompile:
    """Test that the Compass workflow compiles correctly."""

    def test_compass_workflow_compiles(self):
        compiled = compass_workflow.compile()
        assert compiled.name == "compass"

    def test_compass_workflow_has_all_nodes(self):
        compiled = compass_workflow.compile()
        expected_nodes = {
            "classify_task", "check_permissions", "dispatch_task",
            "wait_for_result", "completeness_gate", "summarize_for_user",
            "handle_office_task",
        }
        assert expected_nodes == set(compiled.nodes.keys())

    def test_compass_definition_fields(self):
        from framework.agent import AgentMode, ExecutionMode
        assert compass_definition.agent_id == "compass"
        assert compass_definition.mode == AgentMode.CHAT
        assert compass_definition.execution_mode == ExecutionMode.PERSISTENT


class TestClassifyTask:
    """Test classify_task node with heuristic fallback (no runtime)."""

    async def test_classify_development(self):
        state = {"user_request": "Fix bug in Jira ticket ABC-123"}
        result = await classify_task(state)
        assert result["task_classification"] == "development"

    async def test_classify_office(self):
        state = {"user_request": "Summarize the PDF document in my folder"}
        result = await classify_task(state)
        assert result["task_classification"] == "office"

    async def test_classify_general(self):
        state = {"user_request": "What is the weather today?"}
        result = await classify_task(state)
        assert result["task_classification"] == "general"

    async def test_classify_code_keywords(self):
        state = {"user_request": "Create a new feature branch for implementation"}
        result = await classify_task(state)
        assert result["task_classification"] == "development"


class TestCheckPermissions:

    async def test_permissions_always_allowed_in_mvp(self):
        state = {"task_classification": "development"}
        result = await check_permissions(state)
        assert result["permissions_check"]["allowed"] is True


class TestDispatchTask:

    async def test_dispatch_routes_by_classification(self):
        for classification in ["development", "office", "general"]:
            state = {"task_classification": classification}
            result = await dispatch_task(state)
            assert result["route"] == classification


class TestCompletenessGate:

    async def test_complete_with_pr(self):
        state = {"dev_result": {"pr_url": "https://github.com/test/pr/1", "success": True}}
        result = await completeness_gate(state)
        assert result["route"] == "complete"
        assert result["completeness_score"] == 1.0

    async def test_incomplete_no_pr(self):
        state = {"dev_result": {"success": False}}
        result = await completeness_gate(state)
        assert result["route"] == "incomplete"

    async def test_gives_up_after_retries(self):
        state = {"dev_result": {"success": False}, "_completeness_retries": 2}
        result = await completeness_gate(state)
        assert result["route"] == "complete"  # gives up


class TestSummarizeForUser:

    async def test_summarize_general_no_runtime(self):
        state = {"task_classification": "general", "user_request": "hello"}
        result = await summarize_for_user(state)
        assert "unable" in result["user_summary"].lower()

    async def test_summarize_development(self):
        state = {
            "task_classification": "development",
            "dev_result": {"pr_url": "https://github.com/pr/1", "summary": "Fixed bug."},
        }
        result = await summarize_for_user(state)
        assert "PR:" in result["user_summary"]

    async def test_summarize_office(self):
        state = {
            "task_classification": "office",
            "office_result": {"summary": "3 documents processed."},
        }
        result = await summarize_for_user(state)
        assert "3 documents" in result["user_summary"]


class TestCompassWorkflowExecution:
    """Integration test: run entire workflow with heuristic classification."""

    async def test_general_task_flow(self):
        compiled = compass_workflow.compile()
        state = {"user_request": "What time is it?"}
        result = await compiled.invoke(state)
        assert result["task_classification"] == "general"
        assert "user_summary" in result

    async def test_development_task_flow(self):
        compiled = compass_workflow.compile()
        state = {
            "user_request": "Fix the bug in Jira ticket ABC-123",
            "dev_result": {"pr_url": "https://github.com/pr/1", "success": True},
        }
        result = await compiled.invoke(state)
        assert result["task_classification"] == "development"
        assert result["route"] == "complete"

    async def test_office_task_flow(self):
        compiled = compass_workflow.compile()
        state = {"user_request": "Summarize the PDF files in my documents folder"}
        result = await compiled.invoke(state)
        assert result["task_classification"] == "office"
        assert "user_summary" in result
