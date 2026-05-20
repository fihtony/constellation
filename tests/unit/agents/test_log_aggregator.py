"""Tests for LogAggregator."""
import os
import tempfile
import pytest
from agents.log_store.log_aggregator import LogAggregator, parse_log_line


class TestLogAggregator:
    def test_parse_log_line(self):
        line = "2026-05-20 10:30:00 [INFO ] [team-lead] Task started step=gather_context"
        result = parse_log_line(line)
        assert result["timestamp"] == "2026-05-20 10:30:00"
        assert result["level"] == "INFO"
        assert result["agent"] == "team-lead"
        assert result["message"] == "Task started step=gather_context"

    def test_parse_error_line(self):
        line = "2026-05-20 10:30:00 [ERROR] [scm] Access denied exit_code=403"
        result = parse_log_line(line)
        assert result["level"] == "ERROR"
        assert result["agent"] == "scm"

    def test_aggregate_from_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create agent log file
            agent_dir = os.path.join(tmpdir, "task-123", "team-lead")
            os.makedirs(agent_dir)
            log_file = os.path.join(agent_dir, "agent.log")
            with open(log_file, "w") as f:
                f.write("2026-05-20 10:30:00 [INFO ] [team-lead] Task started\n")

            aggregator = LogAggregator(tmpdir)
            logs = aggregator.aggregate_task("task-123")
            assert len(logs) == 1
            assert logs[0]["agent"] == "team-lead"