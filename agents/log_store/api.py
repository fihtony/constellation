"""LogStore REST API."""
from __future__ import annotations
from typing import Any

from agents.log_store.agent import LogStoreAgent


class LogStoreAPI:
    def __init__(self, agent: LogStoreAgent):
        self.agent = agent

    def get_logs(self, task_id: str) -> dict[str, Any]:
        """GET /logs/{task_id}"""
        return {
            "task_id": task_id,
            "logs": self.agent.get_logs_sync(task_id),
        }

    def add_log(self, task_id: str, log_entry: dict) -> None:
        """Internal method to add a log entry."""
        self.agent.add_log_sync(task_id, log_entry)

    def health(self) -> dict[str, Any]:
        """GET /health"""
        return self.agent.health_sync()