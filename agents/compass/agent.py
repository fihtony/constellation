"""Compass Agent — control plane entry point.

Routes user requests to the appropriate downstream agent (Team Lead for
development tasks, Office Agent for document tasks, or direct LLM for general
questions).  Manages the completeness gate and final user summary.
"""
from __future__ import annotations

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.workflow import Workflow, START, END
from agents.compass.nodes import (
    classify_task,
    check_permissions,
    dispatch_task,
    wait_for_result,
    completeness_gate,
    summarize_for_user,
    handle_office_task,
)

# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

compass_workflow = Workflow(
    name="compass",
    edges=[
        (START, classify_task, check_permissions),
        (check_permissions, dispatch_task),
        (dispatch_task, {
            "development": wait_for_result,
            "office": handle_office_task,
            "general": summarize_for_user,
        }),
        (wait_for_result, completeness_gate),
        (completeness_gate, {
            "complete": summarize_for_user,
            "incomplete": wait_for_result,
        }),
        (handle_office_task, summarize_for_user),
        (summarize_for_user, END),
    ],
)

# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

compass_definition = AgentDefinition(
    agent_id="compass",
    name="Compass Agent",
    description="Control plane: task classification, permission check, routing, and user summary",
    mode=AgentMode.CHAT,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=compass_workflow,
    tools=["dispatch_agent", "query_registry"],
)


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class CompassAgent(BaseAgent):
    """Compass Agent implementation."""

    async def handle_message(self, message: dict) -> dict:
        """Receive a user message, run the compass workflow, return a task dict."""
        from framework.a2a.protocol import Task, TaskState, Message

        session = await self.session_service.create(self.definition.agent_id)
        task = Task(
            task_id=session.session_id,
            state=TaskState.WORKING,
        )

        # Build initial state
        user_text = ""
        msg = message.get("message", message)
        parts = msg.get("parts", [])
        if parts:
            user_text = parts[0].get("text", "")

        state = {
            "_task_id": task.task_id,
            "_session_id": session.session_id,
            "_runtime": self.runtime,
            "_skills_registry": self.skills_registry,
            "user_request": user_text,
            "metadata": msg.get("metadata", {}),
        }

        # Run asynchronously
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
                    "name": "compass-response",
                    "artifactType": "text/plain",
                    "parts": [{"text": result.get("user_summary", "Task completed.")}],
                    "metadata": {"agentId": self.definition.agent_id},
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
        """Return current task state."""
        # In a full implementation, look up task from task store
        return {"task": {"id": task_id, "status": {"state": "TASK_STATE_WORKING"}}}
