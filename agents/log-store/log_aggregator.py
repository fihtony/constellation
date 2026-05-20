"""Filesystem log aggregator for LogStore."""
from __future__ import annotations
import os
import re
from typing import Any


LOG_LINE_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"\[(\w+)\s*\] "
    r"\[([\w-]+)\] "
    r"(.+)$"
)


def parse_log_line(line: str) -> dict[str, Any] | None:
    """Parse a single log line into structured dict."""
    match = LOG_LINE_PATTERN.match(line.strip())
    if not match:
        return None
    timestamp, level, agent, message = match.groups()
    return {
        "timestamp": timestamp,
        "level": level,
        "agent": agent,
        "message": message,
    }


class LogAggregator:
    """Aggregates logs from filesystem for a task."""

    def __init__(self, artifact_root: str = "/artifacts"):
        self.artifact_root = artifact_root

    def get_task_log_dir(self, task_id: str) -> str:
        """Get directory containing all agent logs for a task."""
        return os.path.join(self.artifact_root, task_id)

    def aggregate_task(self, task_id: str) -> list[dict]:
        """Aggregate all logs for a given task from all agents."""
        logs = []
        task_dir = self.get_task_log_dir(task_id)

        if not os.path.isdir(task_dir):
            return logs

        for agent_name in os.listdir(task_dir):
            agent_dir = os.path.join(task_dir, agent_name)
            if not os.path.isdir(agent_dir):
                continue

            log_file = os.path.join(agent_dir, "agent.log")
            if os.path.isfile(log_file):
                agent_logs = self._read_agent_logs(log_file, agent_name)
                logs.extend(agent_logs)

        return sorted(logs, key=lambda x: x["timestamp"])

    def _read_agent_logs(self, log_file: str, agent_name: str) -> list[dict]:
        """Read all log entries from a single agent's log file."""
        logs = []
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    parsed = parse_log_line(line)
                    if parsed:
                        parsed["source"] = agent_name
                        logs.append(parsed)
        except OSError:
            pass
        return logs

    def aggregate_since(
        self, task_id: str, since_timestamp: str | None = None
    ) -> list[dict]:
        """Aggregate logs since a given timestamp."""
        all_logs = self.aggregate_task(task_id)
        if since_timestamp is None:
            return all_logs
        return [log for log in all_logs if log["timestamp"] > since_timestamp]