"""LogStore Agent - Aggregates logs from all agents via filesystem subscription."""
from __future__ import annotations

import json
import time
from typing import Iterable

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

        try:
            log_data = json.loads(text)
            task_id = log_data.get("task_id", "unknown")
            self.add_log_sync(task_id, {
                "timestamp": log_data.get("timestamp", ""),
                "level": log_data.get("level", "INFO"),
                "agent": log_data.get("agent", "unknown"),
                "message": log_data.get("message", ""),
            })
        except json.JSONDecodeError:
            pass

        return {"status": "ok"}

    async def get_logs(self, task_id: str) -> dict:
        """Return logs for a task."""
        return {"task_id": task_id, "logs": self.get_logs_sync(task_id)}

    async def health(self) -> dict:
        return {"status": "ok", "service": "log-store"}

    def get_logs_sync(self, task_id: str) -> list[dict]:
        """Synchronous get logs for API."""
        return self._merged_logs(task_id)

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

    def serve_ui(self, path: str) -> dict:
        """Serve REST and SSE log endpoints for Compass UI consumption."""
        if path.startswith("/logs/stream/"):
            task_id = path[len("/logs/stream/"):]
            return {
                "status": 200,
                "headers": {"Content-Type": "text/event-stream; charset=utf-8"},
                "body": self.stream_logs(task_id),
            }
        if path.startswith("/logs/"):
            task_id = path[len("/logs/"):]
            return {
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body": {"task_id": task_id, "logs": self.get_logs_sync(task_id)},
            }
        return {"status": 404, "body": "Not found"}

    def stream_logs(
        self,
        task_id: str,
        *,
        poll_interval: float = 1.0,
        max_iterations: int | None = None,
    ) -> Iterable[str]:
        """Yield SSE chunks for appended log entries.

        ``max_iterations`` exists so unit tests can exhaust the generator.
        """
        previous = self._merged_logs(task_id)
        for entry in previous:
            yield self._format_sse("log.appended", entry)
        yield ": connected\n\n"

        iterations = 0
        while True:
            if max_iterations is not None and iterations >= max_iterations:
                return
            iterations += 1
            time.sleep(poll_interval)
            current = self._merged_logs(task_id)
            if len(current) > len(previous):
                for entry in current[len(previous):]:
                    yield self._format_sse("log.appended", entry)
            previous = current
            yield ": heartbeat\n\n"

    def _merged_logs(self, task_id: str) -> list[dict]:
        memory_logs = list(self._logs.get(task_id, []))
        filesystem_logs = self.aggregate_from_filesystem(task_id)

        merged: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()
        for entry in [*filesystem_logs, *memory_logs]:
            key = (
                str(entry.get("timestamp", "")),
                str(entry.get("level", "")),
                str(entry.get("agent", "")),
                str(entry.get("message", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append({**entry, "task_id": task_id})

        ordered = sorted(merged, key=lambda item: item.get("timestamp", ""))
        for index, entry in enumerate(ordered, start=1):
            entry.setdefault("sequence", index)
        return ordered

    @staticmethod
    def _format_sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"