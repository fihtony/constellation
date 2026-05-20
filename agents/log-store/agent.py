"""LogStore Agent - Aggregates logs from all agents via filesystem subscription."""
from __future__ import annotations

from framework.agent import AgentDefinition, AgentMode, ExecutionMode


LOGSTORE_DEFINITION = AgentDefinition(
    agent_id="log-store",
    name="Log Store Agent",
    description="Aggregates and serves logs from all agents in a task",
    mode=AgentMode.SERVER,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=[],
)


class LogStoreAgent:
    definition = LOGSTORE_DEFINITION

    def __init__(self, services):
        self.services = services
        self._logs: dict[str, list[dict]] = {}  # task_id -> logs

    async def handle_message(self, message: dict) -> dict:
        """Handle incoming log messages via A2A."""
        parts = message.get("message", {}).get("parts", [])
        text = next((p.get("text", "") for p in parts if p.get("text")), "")
        # Parse log entry
        # Store in self._logs[task_id]
        return {"status": "ok"}

    async def get_logs(self, task_id: str) -> dict:
        """Return logs for a task."""
        return {"task_id": task_id, "logs": self._logs.get(task_id, [])}

    async def health(self) -> dict:
        return {"status": "ok", "service": "log-store"}