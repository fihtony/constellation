"""Code Review Agent — independent code quality review.

Reviews PR diffs for code quality, security, test coverage, and
requirements compliance.  Returns a structured verdict.
"""
from __future__ import annotations

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.workflow import Workflow, START, END
from agents.code_review.nodes import (
    load_pr_context,
    review_quality,
    review_security,
    review_tests,
    review_requirements,
    generate_report,
)

# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

code_review_workflow = Workflow(
    name="code_review",
    edges=[
        (START, load_pr_context, review_quality),
        (review_quality, review_security),
        (review_security, review_tests),
        (review_tests, review_requirements),
        (review_requirements, generate_report),
        (generate_report, END),
    ],
)

# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

code_review_definition = AgentDefinition(
    agent_id="code-review",
    name="Code Review Agent",
    description="Independent code review: quality, security, tests, requirements compliance",
    mode=AgentMode.TASK,
    execution_mode=ExecutionMode.PER_TASK,
    workflow=code_review_workflow,
    skills=["code-review"],
    tools=["read_file", "search_code"],
    permissions={"scm": "read"},
)


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class CodeReviewAgent(BaseAgent):
    """Code Review Agent implementation."""

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Task, TaskState

        session = await self.session_service.create(self.definition.agent_id)
        task = Task(task_id=session.session_id, state=TaskState.WORKING)

        msg = message.get("message", message)
        metadata = msg.get("metadata", {})

        state = {
            "_task_id": task.task_id,
            "_session_id": session.session_id,
            "_runtime": self.runtime,
            "_skills_registry": self.skills_registry,
            "pr_url": metadata.get("prUrl", ""),
            "repo_url": metadata.get("repoUrl", ""),
            "jira_context": metadata.get("jiraContext", {}),
            "original_requirements": metadata.get("originalRequirements", ""),
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
                    "name": "code-review-report",
                    "artifactType": "application/json",
                    "parts": [{"text": json.dumps({
                        "verdict": result.get("verdict", "rejected"),
                        "comments": result.get("all_comments", []),
                        "summary": result.get("report_summary", ""),
                    })}],
                    "metadata": {"agentId": self.definition.agent_id},
                }]
            except Exception as e:
                task.state = TaskState.FAILED
                task.status_message = str(e)
            finally:
                loop.close()

        import json
        worker = threading.Thread(target=_run, daemon=True)
        worker.start()

        return task.to_dict()

    async def get_task(self, task_id: str) -> dict:
        return {"task": {"id": task_id, "status": {"state": "TASK_STATE_WORKING"}}}
