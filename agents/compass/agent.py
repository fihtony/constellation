"""Compass Agent -- LLM-driven control plane entry point.

Architecture: **ReAct-first** (appropriate for open-ended user interaction).

Uses the ReAct pattern (run_agentic + tools) for user request classification
and routing.  The LLM decides what to do next based on the user request and
the registered tools.

All tasks are persisted via TaskStore so that ``GET /tasks/{id}`` returns
real state (not a stub).

Instructions (system prompt) live in:
  agents/compass/instructions/system.md

Tools live in:
  agents/compass/tools.py
"""
from __future__ import annotations

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from agents.compass.tools import TOOL_NAMES, register_compass_tools

compass_definition = AgentDefinition(
    agent_id="compass",
    name="Compass Agent",
    description="Control plane: task classification, routing, and user summary (ReAct-first)",
    mode=AgentMode.CHAT,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=TOOL_NAMES,
)


class CompassAgent(BaseAgent):
    """Compass Agent -- routes requests via LLM ReAct reasoning."""

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact, TaskState
        from framework.instructions import load_instructions
        from framework.runtime.adapter import get_runtime

        register_compass_tools()

        msg = message.get("message", message)
        parts = msg.get("parts") or []
        user_text = next((p.get("text", "") for p in parts if p.get("text")), "")

        # Create task via TaskStore
        task_store = self.services.task_store
        task = task_store.create_task(agent_id=self.definition.agent_id)

        system_prompt = load_instructions("compass")
        runtime = self.services.runtime or get_runtime()
        agentic_result = runtime.run_agentic(
            task=user_text,
            tools=TOOL_NAMES,
            system_prompt=system_prompt,
            max_turns=20,
            timeout=300,
        )

        artifacts = [Artifact(
            name="compass-response",
            artifact_type="text/plain",
            parts=[{"text": agentic_result.summary or ""}],
            metadata={"agentId": "compass"},
        )]

        if agentic_result.success:
            task_store.complete_task(task.id, artifacts=artifacts)
        else:
            task_store.fail_task(task.id, agentic_result.summary or "Agent failed")

        return task_store.get_task_dict(task.id)

    async def get_task(self, task_id: str) -> dict:
        """Return real task state from TaskStore."""
        return self.services.task_store.get_task_dict(task_id)
