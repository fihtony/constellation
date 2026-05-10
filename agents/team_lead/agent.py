"""Team Lead Agent — intelligence layer.

Analyzes tasks, gathers context (Jira, design), creates plans, dispatches
dev agents, coordinates code review, and reports results.
"""
from __future__ import annotations

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.workflow import Workflow, START, END
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

# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

team_lead_workflow = Workflow(
    name="team_lead",
    edges=[
        (START, receive_task, analyze_requirements),
        (analyze_requirements, gather_context),
        (gather_context, create_plan),
        (create_plan, select_skills),
        (select_skills, dispatch_dev_agent),
        (dispatch_dev_agent, wait_for_dev),
        (wait_for_dev, {
            "completed": dispatch_code_review,
            "needs_clarification": handle_question,
            "failed": escalate_to_user,
        }),
        (handle_question, {
            "self_answered": dispatch_dev_agent,
            "user_needed": escalate_to_user,
        }),
        (dispatch_code_review, evaluate_review),
        (evaluate_review, {
            "approved": report_success,
            "needs_revision": request_revision,
            "max_revisions": escalate_to_user,
        }),
        (request_revision, dispatch_dev_agent),
        (report_success, END),
        (escalate_to_user, END),
    ],
)

# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

team_lead_definition = AgentDefinition(
    agent_id="team-lead",
    name="Team Lead Agent",
    description="Intelligence layer: analysis, planning, dispatch, code review coordination",
    mode=AgentMode.TASK,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=team_lead_workflow,
    tools=["dispatch_agent", "query_registry", "jira_fetch", "design_fetch"],
)


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class TeamLeadAgent(BaseAgent):
    """Team Lead Agent implementation."""

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Task, TaskState

        session = await self.session_service.create(self.definition.agent_id)
        task = Task(task_id=session.session_id, state=TaskState.WORKING)

        msg = message.get("message", message)
        parts = msg.get("parts", [])
        user_text = parts[0].get("text", "") if parts else ""
        metadata = msg.get("metadata", {})

        state = {
            "_task_id": task.task_id,
            "_session_id": session.session_id,
            "_runtime": self.runtime,
            "_skills_registry": self.skills_registry,
            "user_request": user_text,
            "jira_key": metadata.get("jiraKey", ""),
            "repo_url": metadata.get("repoUrl", ""),
            "figma_url": metadata.get("figmaUrl", ""),
            "max_review_cycles": 2,
            "review_cycles": 0,
            "metadata": metadata,
        }

        import threading

        def _run():
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    self._compiled_workflow.invoke(state)
                )
                task.state = TaskState.COMPLETED
                task.artifacts = [{
                    "name": "team-lead-result",
                    "artifactType": "text/plain",
                    "parts": [{"text": result.get("summary", "Task completed.")}],
                    "metadata": {
                        "agentId": self.definition.agent_id,
                        "prUrl": result.get("pr_url", ""),
                        "branch": result.get("branch_name", ""),
                    },
                }]
            except Exception as e:
                task.state = TaskState.FAILED
                task.status_message = str(e)
            finally:
                loop.close()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()

        return task.to_dict()

    async def get_task(self, task_id: str) -> dict:
        return {"task": {"id": task_id, "status": {"state": "TASK_STATE_WORKING"}}}
