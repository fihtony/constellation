"""Web Dev Agent — full-stack development execution.

Clones a repository, creates a branch, implements changes, runs tests,
fixes failures, creates a PR, and reports results back to Team Lead.
"""
from __future__ import annotations

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.workflow import Workflow, START, END
from agents.web_dev.nodes import (
    setup_workspace,
    analyze_task,
    implement_changes,
    run_tests,
    fix_tests,
    create_pr,
    report_result,
)

# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

web_dev_workflow = Workflow(
    name="web_dev",
    edges=[
        (START, setup_workspace, analyze_task),
        (analyze_task, implement_changes),
        (implement_changes, run_tests),
        (run_tests, {
            "pass": create_pr,
            "fail": fix_tests,
        }),
        (fix_tests, run_tests),
        (create_pr, report_result),
        (report_result, END),
    ],
)

# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

web_dev_definition = AgentDefinition(
    agent_id="web-dev",
    name="Web Dev Agent",
    description="Full-stack web development: clone, branch, implement, test, PR",
    mode=AgentMode.TASK,
    execution_mode=ExecutionMode.PER_TASK,
    workflow=web_dev_workflow,
    skills=["react-nextjs", "testing"],
    tools=[
        "read_file", "write_file", "edit_file", "search_code", "run_command",
        "scm_clone", "scm_branch", "scm_commit", "scm_push", "scm_create_pr",
    ],
    permissions={"scm": "read-write", "filesystem": "workspace-only"},
)


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class WebDevAgent(BaseAgent):
    """Web Dev Agent implementation."""

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
            "repo_url": metadata.get("repoUrl", ""),
            "branch_name": metadata.get("branchName", ""),
            "jira_context": metadata.get("jiraContext", {}),
            "design_context": metadata.get("designContext"),
            "skill_context": metadata.get("skillContext", ""),
            "task_type": metadata.get("taskType", "general"),
            "analysis": metadata.get("analysis", ""),
            "test_cycles": 0,
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
                    "name": "web-dev-result",
                    "artifactType": "text/plain",
                    "parts": [{"text": result.get("implementation_summary", "Done.")}],
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
