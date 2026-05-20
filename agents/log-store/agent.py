"""LogStore Agent - Aggregates logs from all agents via filesystem subscription."""
from __future__ import annotations

from framework.agent import AgentDefinition, AgentMode, ExecutionMode
from agents.log_store.log_aggregator import LogAggregator


LOGSTORE_DEFINITION = AgentDefinition(
    agent_id="log-store",
    name="Log Store Agent",
    description="Aggregates and serves logs from all agents in a task",
    mode=AgentMode.TASK,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=[],
)


class LogStoreAgent:
    definition = LOGSTORE_DEFINITION

    def __init__(self, services, artifact_root: str = "/artifacts"):
        self.services = services
        self._logs: dict[str, list[dict]] = {}
        self._aggregator = LogAggregator(artifact_root)

    def aggregate_from_filesystem(self, task_id: str) -> list[dict]:
        """Called periodically to sync filesystem logs."""
        return self._aggregator.aggregate_task(task_id)

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

    def get_logs_sync(self, task_id: str) -> list[dict]:
        """Synchronous get logs for API."""
        return self._logs.get(task_id, [])

    def add_log_sync(self, task_id: str, log_entry: dict) -> None:
        """Synchronous add log for API."""
        if task_id not in self._logs:
            self._logs[task_id] = []
        self._logs[task_id].append(log_entry)

    def health_sync(self) -> dict:
        """Synchronous health check for API."""
        return {"status": "ok", "service": "log-store"}

    def get_api(self) -> "LogStoreAPI":
        """Return API instance for REST endpoints."""
        from agents.log_store.api import LogStoreAPI
        return LogStoreAPI(self)