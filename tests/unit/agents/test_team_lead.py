"""Tests for Team Lead Agent workflow."""
import pytest
from framework.workflow import START, END
from framework.errors import InterruptSignal
from agents.team_lead.agent import team_lead_workflow, team_lead_definition
from agents.team_lead.nodes import (
    receive_task,
    analyze_requirements,
    gather_context,
    create_plan,
    select_skills,
    dispatch_dev_agent,
    wait_for_dev,
    dispatch_code_review,
    evaluate_review,
    request_revision,
    handle_question,
    report_success,
    escalate_to_user,
)


class TestTeamLeadWorkflowCompile:

    def test_team_lead_workflow_compiles(self):
        compiled = team_lead_workflow.compile()
        assert compiled.name == "team_lead"

    def test_team_lead_workflow_has_all_nodes(self):
        compiled = team_lead_workflow.compile()
        expected_nodes = {
            "receive_task", "analyze_requirements", "gather_context",
            "create_plan", "select_skills", "dispatch_dev_agent",
            "wait_for_dev", "dispatch_code_review", "evaluate_review",
            "request_revision", "handle_question", "report_success",
            "escalate_to_user",
        }
        assert expected_nodes == set(compiled.nodes.keys())

    def test_team_lead_definition_fields(self):
        from framework.agent import AgentMode, ExecutionMode
        assert team_lead_definition.agent_id == "team-lead"
        assert team_lead_definition.mode == AgentMode.TASK
        assert team_lead_definition.execution_mode == ExecutionMode.PERSISTENT


class TestTeamLeadNodes:

    async def test_receive_task(self):
        state = {"jira_key": "ABC-123", "repo_url": "https://github.com/test"}
        result = await receive_task(state)
        assert result["task_received"] is True
        assert result["jira_key"] == "ABC-123"

    async def test_analyze_requirements_no_runtime(self):
        state = {"user_request": "Implement new login page"}
        result = await analyze_requirements(state)
        assert result["task_type"] == "general"
        assert result["complexity"] == "medium"

    async def test_gather_context_with_jira(self):
        state = {"jira_key": "TEST-1"}
        result = await gather_context(state)
        assert result["jira_context"]["key"] == "TEST-1"

    async def test_gather_context_with_figma(self):
        state = {"figma_url": "https://figma.com/design/123"}
        result = await gather_context(state)
        assert result["design_context"] is not None

    async def test_create_plan_no_runtime(self):
        state = {}
        result = await create_plan(state)
        assert "steps" in result["plan"]

    async def test_select_skills_no_registry(self):
        state = {"required_skills": ["react-nextjs"]}
        result = await select_skills(state)
        assert result["skill_context"] == ""

    async def test_wait_for_dev_completed(self):
        state = {"dev_result": {"state": "TASK_STATE_COMPLETED", "pr_url": "https://pr/1"}}
        result = await wait_for_dev(state)
        assert result["route"] == "completed"

    async def test_wait_for_dev_failed(self):
        state = {"dev_result": {}}
        result = await wait_for_dev(state)
        assert result["route"] == "failed"

    async def test_handle_question_escalates(self):
        state = {}
        result = await handle_question(state)
        assert result["route"] == "user_needed"

    async def test_evaluate_review_approved(self):
        state = {"review_verdict": "approved", "review_cycles": 0}
        result = await evaluate_review(state)
        assert result["route"] == "approved"

    async def test_evaluate_review_needs_revision(self):
        state = {"review_verdict": "rejected", "review_cycles": 0, "max_review_cycles": 2, "review_comments": []}
        result = await evaluate_review(state)
        assert result["route"] == "needs_revision"

    async def test_evaluate_review_max_revisions(self):
        state = {"review_verdict": "rejected", "review_cycles": 2, "max_review_cycles": 2, "review_comments": []}
        result = await evaluate_review(state)
        assert result["route"] == "max_revisions"

    async def test_report_success(self):
        state = {"pr_url": "https://pr/1"}
        result = await report_success(state)
        assert result["success"] is True

    async def test_escalate_to_user_interrupts(self):
        state = {"escalation_reason": "Tests failing"}
        with pytest.raises(InterruptSignal) as exc_info:
            await escalate_to_user(state)
        assert "Tests failing" in exc_info.value.question


class TestTeamLeadWorkflowExecution:

    async def test_happy_path_approved(self):
        """Full happy path: task → analyze → gather → plan → skills → dev → review → success."""
        compiled = team_lead_workflow.compile()
        state = {
            "user_request": "Add login page",
            "jira_key": "TEST-1",
            "repo_url": "https://github.com/test",
            "max_review_cycles": 2,
            "review_cycles": 0,
            "dev_result": {"state": "TASK_STATE_COMPLETED", "pr_url": "https://pr/1"},
        }
        result = await compiled.invoke(state)
        assert result["success"] is True
        assert "PR:" in result["summary"]

    async def test_escalation_on_failure(self):
        """Dev agent fails → escalate → interrupt."""
        compiled = team_lead_workflow.compile()
        state = {
            "user_request": "Fix bug",
            "max_review_cycles": 2,
            "review_cycles": 0,
            "dev_result": {"state": "TASK_STATE_FAILED"},
        }
        with pytest.raises(InterruptSignal):
            await compiled.invoke(state)
