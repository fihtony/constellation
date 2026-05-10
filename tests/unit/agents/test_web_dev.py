"""Tests for Web Dev Agent workflow."""
import pytest
from framework.workflow import START, END
from agents.web_dev.agent import web_dev_workflow, web_dev_definition
from agents.web_dev.nodes import (
    setup_workspace,
    analyze_task,
    implement_changes,
    run_tests,
    fix_tests,
    create_pr,
    report_result,
)


class TestWebDevWorkflowCompile:

    def test_web_dev_workflow_compiles(self):
        compiled = web_dev_workflow.compile()
        assert compiled.name == "web_dev"

    def test_web_dev_workflow_has_all_nodes(self):
        compiled = web_dev_workflow.compile()
        expected_nodes = {
            "setup_workspace", "analyze_task", "implement_changes",
            "run_tests", "fix_tests", "create_pr", "report_result",
        }
        assert expected_nodes == set(compiled.nodes.keys())

    def test_web_dev_definition_fields(self):
        from framework.agent import AgentMode, ExecutionMode
        assert web_dev_definition.agent_id == "web-dev"
        assert web_dev_definition.mode == AgentMode.TASK
        assert web_dev_definition.execution_mode == ExecutionMode.PER_TASK
        assert "react-nextjs" in web_dev_definition.skills


class TestWebDevNodes:

    async def test_setup_workspace(self):
        state = {"_task_id": "test-123", "repo_url": "https://github.com/test", "branch_name": "fix/test"}
        result = await setup_workspace(state)
        assert "workspace_path" in result
        assert result["branch_created"] is True

    async def test_analyze_task(self):
        state = {"analysis": "Implement login feature", "user_request": "Add login"}
        result = await analyze_task(state)
        assert result["implementation_plan"] == "Implement login feature"

    async def test_implement_changes(self):
        state = {}
        result = await implement_changes(state)
        assert "changes_made" in result

    async def test_run_tests_pass(self):
        state = {"test_cycles": 0}
        result = await run_tests(state)
        assert result["route"] == "pass"
        assert result["test_status"] == "pass"

    async def test_fix_tests(self):
        state = {}
        result = await fix_tests(state)
        assert result["fix_attempted"] is True

    async def test_create_pr(self):
        state = {}
        result = await create_pr(state)
        assert "pr_url" in result

    async def test_report_result(self):
        state = {"pr_url": "https://pr/1", "changes_made": ["a.py", "b.py"]}
        result = await report_result(state)
        assert result["success"] is True
        assert result["state"] == "TASK_STATE_COMPLETED"


class TestWebDevWorkflowExecution:

    async def test_happy_path(self):
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
