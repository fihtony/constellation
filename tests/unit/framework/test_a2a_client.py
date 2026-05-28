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

    @patch("framework.a2a.client.urllib.request.urlopen")
    def test_initial_post_uses_total_timeout_budget(self, mock_urlopen):
        """dispatch_sync should let a synchronous boundary handler use the configured timeout budget."""
        send_resp = self._mock_response("TASK_STATE_COMPLETED", "t-005")
        mock_urlopen.side_effect = [send_resp]

        dispatch_sync(
            url="http://fake:8000",
            capability="test.cap",
            message_parts=[{"text": "hello"}],
            timeout=120,
            poll_interval=0,
        )

        assert mock_urlopen.call_args_list[0].kwargs["timeout"] == 120

    @patch("framework.a2a.client.current_permission_snapshot")
    @patch("framework.a2a.client.urllib.request.urlopen")
    def test_dispatch_sync_attaches_current_permissions(self, mock_urlopen, mock_snapshot):
        """dispatch_sync should attach the caller permission snapshot to outbound metadata."""
        mock_snapshot.return_value = {
            "allowedTools": ["fetch_jira_ticket"],
            "scm": "read-write",
            "filesystem": "workspace-only",
        }
        send_resp = self._mock_response("TASK_STATE_COMPLETED", "t-006")
        mock_urlopen.side_effect = [send_resp]

        dispatch_sync(
            url="http://fake:8000",
            capability="jira.ticket.fetch",
            message_parts=[{"text": "PROJ-123"}],
            metadata={"ticketKey": "PROJ-123"},
            poll_interval=0,
        )

        request = mock_urlopen.call_args_list[0].args[0]
        payload = json.loads(request.data.decode("utf-8"))
        metadata = payload["message"]["metadata"]
        assert metadata["ticketKey"] == "PROJ-123"
        assert metadata["requestedCapability"] == "jira.ticket.fetch"
        assert metadata["permissions"]["allowedTools"] == ["fetch_jira_ticket"]

    @patch("framework.a2a.client.current_permission_snapshot")
    @patch("framework.a2a.client.urllib.request.urlopen")
    def test_dispatch_sync_preserves_explicit_permissions(self, mock_urlopen, mock_snapshot):
        """dispatch_sync must not overwrite an explicit permission snapshot."""
        mock_snapshot.return_value = {"allowedTools": ["wrong"]}
        send_resp = self._mock_response("TASK_STATE_COMPLETED", "t-007")
        mock_urlopen.side_effect = [send_resp]

        dispatch_sync(
            url="http://fake:8000",
            capability="jira.ticket.fetch",
            message_parts=[{"text": "PROJ-123"}],
            metadata={
                "ticketKey": "PROJ-123",
                "permissions": {"allowedTools": ["fetch_jira_ticket"], "scm": "read"},
            },
            poll_interval=0,
        )

        request = mock_urlopen.call_args_list[0].args[0]
        payload = json.loads(request.data.decode("utf-8"))
        metadata = payload["message"]["metadata"]
        assert metadata["permissions"] == {"allowedTools": ["fetch_jira_ticket"], "scm": "read"}


class TestA2AClientAsync:
    """Test async A2AClient methods."""

    @pytest.mark.asyncio
    async def test_dispatch_no_url_raises(self):
        client = A2AClient()
        with pytest.raises(ValueError, match="Either url or"):
            await client.dispatch(message={"text": "hi"})

    @pytest.mark.asyncio
    @patch("framework.a2a.client.current_permission_snapshot")
    @patch("framework.a2a.client.A2AClient._http_post")
    async def test_async_dispatch_envelope_attaches_current_permissions(self, mock_post, mock_snapshot):
        mock_snapshot.return_value = {
            "allowedTools": ["fetch_jira_ticket"],
            "scm": "read-write",
            "filesystem": "workspace-only",
        }
        mock_post.return_value = {
            "task": {
                "id": "t-008",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [],
            }
        }

        client = A2AClient()
        await client.dispatch(
            url="http://fake:8000",
            message={"text": "hello", "metadata": {"ticketKey": "PROJ-123"}},
            wait=False,
        )

        envelope = mock_post.call_args.args[1]
        metadata = envelope["message"]["metadata"]
        assert metadata["ticketKey"] == "PROJ-123"
        assert metadata["permissions"]["allowedTools"] == ["fetch_jira_ticket"]
