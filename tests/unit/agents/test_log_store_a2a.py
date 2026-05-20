"""Tests for LogStore A2A log reception."""
import pytest
from agents.log_store.agent import LogStoreAgent


class TestLogStoreA2A:
    async def test_receive_error_log(self):
        agent = LogStoreAgent(services=None)
        message = {
            "message": {
                "parts": [{
                    "text": '{"task_id":"task-123","level":"ERROR","agent":"scm","message":"Access denied"}'
                }]
            }
        }
        result = await agent.handle_message(message)
        assert result["status"] == "ok"
        logs = agent.get_logs_sync("task-123")
        assert len(logs) == 1
        assert logs[0]["level"] == "ERROR"