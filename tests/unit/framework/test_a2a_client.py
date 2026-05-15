"""Tests for A2A client — dispatch_sync terminal states."""
import json
from unittest.mock import patch, MagicMock

import pytest

from framework.a2a.client import A2AClient, dispatch_sync


class TestDispatchSyncTerminalStates:
    """Verify that dispatch_sync treats INPUT_REQUIRED as a sync terminal state."""

    def _mock_response(self, state: str, task_id: str = "t-001"):
        data = json.dumps({
            "task": {
                "id": task_id,
                "status": {"state": state},
                "artifacts": [],
            }
        }).encode()
        resp = MagicMock()
        resp.read.return_value = data
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("framework.a2a.client.urllib.request.urlopen")
    def test_input_required_is_terminal(self, mock_urlopen):
        """dispatch_sync should return immediately on INPUT_REQUIRED."""
        # First call: POST /message:send → returns task in WORKING
        send_resp = self._mock_response("TASK_STATE_WORKING", "t-001")
        # Second call: GET /tasks/t-001 → returns INPUT_REQUIRED
        poll_resp = self._mock_response("TASK_STATE_INPUT_REQUIRED", "t-001")
        mock_urlopen.side_effect = [send_resp, poll_resp]

        result = dispatch_sync(
            url="http://fake:8000",
            capability="test.cap",
            message_parts=[{"text": "hello"}],
            poll_interval=0,
        )
        task = result.get("task", result)
        assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

    @patch("framework.a2a.client.urllib.request.urlopen")
    def test_completed_is_terminal(self, mock_urlopen):
        """dispatch_sync should return on COMPLETED."""
        send_resp = self._mock_response("TASK_STATE_WORKING", "t-002")
        poll_resp = self._mock_response("TASK_STATE_COMPLETED", "t-002")
        mock_urlopen.side_effect = [send_resp, poll_resp]

        result = dispatch_sync(
            url="http://fake:8000",
            capability="test.cap",
            message_parts=[{"text": "hello"}],
            poll_interval=0,
        )
        task = result.get("task", result)
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"

    @patch("framework.a2a.client.urllib.request.urlopen")
    def test_failed_is_terminal(self, mock_urlopen):
        """dispatch_sync should return on FAILED."""
        send_resp = self._mock_response("TASK_STATE_WORKING", "t-003")
        poll_resp = self._mock_response("TASK_STATE_FAILED", "t-003")
        mock_urlopen.side_effect = [send_resp, poll_resp]

        result = dispatch_sync(
            url="http://fake:8000",
            capability="test.cap",
            message_parts=[{"text": "hello"}],
            poll_interval=0,
        )
        task = result.get("task", result)
        assert task["status"]["state"] == "TASK_STATE_FAILED"

    @patch("framework.a2a.client.urllib.request.urlopen")
    def test_cancelled_is_terminal(self, mock_urlopen):
        """dispatch_sync should return on CANCELLED."""
        send_resp = self._mock_response("TASK_STATE_WORKING", "t-004")
        poll_resp = self._mock_response("TASK_STATE_CANCELLED", "t-004")
        mock_urlopen.side_effect = [send_resp, poll_resp]

        result = dispatch_sync(
            url="http://fake:8000",
            capability="test.cap",
            message_parts=[{"text": "hello"}],
            poll_interval=0,
        )
        task = result.get("task", result)
        assert task["status"]["state"] == "TASK_STATE_CANCELLED"


class TestA2AClientAsync:
    """Test async A2AClient methods."""

    @pytest.mark.asyncio
    async def test_dispatch_no_url_raises(self):
        client = A2AClient()
        with pytest.raises(ValueError, match="Either url or"):
            await client.dispatch(message={"text": "hi"})
