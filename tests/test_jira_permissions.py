#!/usr/bin/env python3
"""Focused unit tests for Jira permission enforcement helpers."""

from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any, cast
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from common.task_permissions import load_permission_grant
from jira import app as jira_app


class _FakeHandler:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}
        self.sent_code: int | None = None
        self.sent_body: dict | None = None

    def _send_json(self, code: int, body: dict):
        self.sent_code = code
        self.sent_body = body


class _FakeMessageSendHandler(_FakeHandler):
    def __init__(self, body: dict, headers: dict[str, str] | None = None):
        super().__init__(headers=headers)
        self.path = "/message:send"
        self._body = body

    def _read_body(self) -> dict:
        return self._body


_DEVELOPMENT_PERMISSIONS = load_permission_grant("development").to_dict()


def _cleanup_permissions() -> dict:
    grant = copy.deepcopy(_DEVELOPMENT_PERMISSIONS)
    filtered_denied = []
    for entry in grant.get("denied", []):
        if entry.get("agent") != "jira":
            filtered_denied.append(entry)
            continue
        operations = [
            op for op in entry.get("operations", [])
            if op.get("action") != "comment.delete"
        ]
        if operations:
            new_entry = dict(entry)
            new_entry["operations"] = operations
            filtered_denied.append(new_entry)
    grant["denied"] = filtered_denied
    grant.setdefault("allowed", []).append(
        {
            "agent": "jira",
            "operations": [{"action": "comment.delete", "scope": "*"}],
        }
    )
    return grant


def test_mutation_without_permissions_is_denied():
    handler = _FakeHandler()
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = jira_app._enforce_jira_permission(
            handler,
            action="comment.add",
            target="PROJ-2903",
        )
    assert not allowed
    assert handler.sent_code == 403
    assert handler.sent_body is not None
    assert handler.sent_body.get("error") == "permission_denied"


def test_read_without_permissions_is_denied():
    handler = _FakeHandler()
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = jira_app._enforce_jira_permission(
            handler,
            action="read",
            target="PROJ-2903",
        )
    assert not allowed
    assert handler.sent_code == 403


def test_labels_update_allowed_with_development_grant():
    handler = _FakeHandler()
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = jira_app._enforce_jira_permission(
            handler,
            action="issue.update.labels",
            target="PROJ-2903",
            payload_permissions=_DEVELOPMENT_PERMISSIONS,
            response_key="field",
            response_value="labels",
        )
    assert allowed
    assert handler.sent_code is None


def test_read_allowed_with_development_grant():
    handler = _FakeHandler()
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = jira_app._enforce_jira_permission(
            handler,
            action="read",
            target="PROJ-2903",
            payload_permissions=_DEVELOPMENT_PERMISSIONS,
        )
    assert allowed
    assert handler.sent_code is None


def test_comment_delete_denied_with_development_grant():
    handler = _FakeHandler()
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = jira_app._enforce_jira_permission(
            handler,
            action="comment.delete",
            target="PROJ-2903/12345",
            payload_permissions=_DEVELOPMENT_PERMISSIONS,
        )
    assert not allowed
    assert handler.sent_code == 403


def test_comment_delete_allowed_with_cleanup_header():
    header_permissions = json.dumps(_cleanup_permissions(), ensure_ascii=False)
    handler = _FakeHandler(headers={"X-Task-Permissions": header_permissions})
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = jira_app._enforce_jira_permission(
            handler,
            action="comment.delete",
            target="PROJ-2903/12345",
        )
    assert allowed
    assert handler.sent_code is None


def test_invalid_permission_header_is_denied():
    handler = _FakeHandler(headers={"X-Task-Permissions": "{not-json}"})
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = jira_app._enforce_jira_permission(
            handler,
            action="assignee.update",
            target="PROJ-2903",
        )
    assert not allowed
    assert handler.sent_code == 403
    assert "Invalid X-Task-Permissions header" in (handler.sent_body or {}).get("reason", "")


def test_message_send_defaults_to_async_runtime_path():
    handler = _FakeMessageSendHandler(
        {
            "message": {
                "parts": [{"text": "lookup ticket PROJ-2903"}],
                "metadata": {
                    "requestedCapability": "jira.issue.lookup",
                    "permissions": _DEVELOPMENT_PERMISSIONS,
                },
            }
        }
    )

    class _FakeThread:
        def __init__(self, *, target=None, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.started = False
            created_threads.append(self)

        def start(self):
            self.started = True

    created_threads: list[_FakeThread] = []

    with patch.object(jira_app.threading, "Thread", side_effect=lambda *args, **kwargs: _FakeThread(**kwargs)):
        jira_app.JiraHandler.do_POST(cast(Any, handler))

    assert handler.sent_code == 200
    assert handler.sent_body is not None
    task = handler.sent_body["task"]
    assert task["status"]["state"] == "TASK_STATE_ACCEPTED"
    assert len(created_threads) == 1
    assert created_threads[0].target is jira_app._run_task_async
    assert created_threads[0].started is True


def test_a2a_comment_add_uses_requested_capability_and_metadata_permissions():
    from common.tools.registry import get_tool
    import jira.provider_tools as _jpt
    message = {
        "parts": [{"text": "Add comment to ticket PROJ-2903: hello from review"}],
        "metadata": {
            "requestedCapability": "jira.comment.add",
            "ticketKey": "PROJ-2903",
            "commentText": "hello from review",
            "permissions": _DEVELOPMENT_PERMISSIONS,
        },
    }
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        _jpt.configure_jira_provider_tools(
            message=message,
            provider=jira_app.PROVIDER,
            permission_fn=None,  # no permission enforcement — this test checks provider call, not permissions
        )
        tool = get_tool("jira_comment")
        with patch.object(
            jira_app.PROVIDER,
            "add_comment",
            return_value=("101", "created"),
        ) as mock_add_comment:
            result = tool.execute({"ticket_key": "PROJ-2903", "body": "hello from review"})

    mock_add_comment.assert_called_once_with("PROJ-2903", "hello from review")
    text = result["content"][0]["text"]
    assert "101" in text or "PROJ-2903" in text


def test_a2a_transition_uses_requested_capability_and_provider():
    from common.tools.registry import get_tool, _registry, register_tool, snapshot_registry, restore_registry
    import jira.provider_tools as _jpt
    snap = snapshot_registry()
    try:
        # Ensure the provider version is registered (not the boundary A2A version)
        for t in _jpt._TOOLS:
            if t.schema.name == "jira_transition":
                _registry.pop("jira_transition", None)
                register_tool(t)
                break
        message = {
            "parts": [{"text": "Transition ticket PROJ-2903 to In Review"}],
            "metadata": {
                "requestedCapability": "jira.ticket.transition",
                "ticketKey": "PROJ-2903",
                "transition": "In Review",
                "permissions": _DEVELOPMENT_PERMISSIONS,
            },
        }
        with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
            _jpt.configure_jira_provider_tools(
                message=message,
                provider=jira_app.PROVIDER,
                permission_fn=None,
            )
            tool = get_tool("jira_transition")
            with patch.object(
                jira_app.PROVIDER,
                "transition_issue",
                return_value=("31", "ok"),
            ) as mock_transition:
                result = tool.execute({"ticket_key": "PROJ-2903", "transition_name": "In Review"})

        mock_transition.assert_called_once_with("PROJ-2903", "In Review")
        text = result["content"][0]["text"]
        assert "PROJ-2903" in text or "31" in text
    finally:
        restore_registry(snap)


def test_a2a_myself_returns_json_artifact():
    from common.tools.registry import get_tool, _registry, register_tool, snapshot_registry, restore_registry
    import jira.provider_tools as _jpt
    snap = snapshot_registry()
    try:
        # Ensure the provider version is registered (not the boundary A2A version)
        for t in _jpt._TOOLS:
            if t.schema.name == "jira_get_myself":
                _registry.pop("jira_get_myself", None)
                register_tool(t)
                break
        message = {
            "parts": [{"text": "Who am I?"}],
            "metadata": {
                "requestedCapability": "jira.user.myself",
                "permissions": _DEVELOPMENT_PERMISSIONS,
            },
        }
        with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
            _jpt.configure_jira_provider_tools(
                message=message,
                provider=jira_app.PROVIDER,
                permission_fn=None,
            )
            tool = get_tool("jira_get_myself")
            with patch.object(
                jira_app.PROVIDER,
                "get_myself",
                return_value=({"accountId": "abc-123", "displayName": "Svc"}, "ok"),
            ) as mock_myself:
                result = tool.execute({})

        mock_myself.assert_called_once_with()
        text = result["content"][0]["text"]
        assert "abc-123" in text
    finally:
        restore_registry(snap)


def test_jira_audit_log_written_on_write_operation(tmp_path):
    """_write_jira_audit() must append a JSONL entry to the workspace audit file."""
    from jira.app import _write_jira_audit
    workspace = str(tmp_path)
    audit_path = tmp_path / "jira-agent" / "audit-log.jsonl"
    message = {
        "metadata": {
            "requestingAgent": "team-lead",
            "orchestratorTaskId": "t-audit-1",
        }
    }

    _write_jira_audit(
        workspace_path=workspace,
        message=message,
        operation="jira.comment.add",
        target="PROJ-42",
        input_summary={"commentBody": "test comment"},
        result={"success": True},
        duration_ms=123,
    )

    assert audit_path.exists(), "jira audit-log.jsonl was not created"
    import json
    lines = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["agentId"] == "jira-agent"
    assert entry["requestingTaskId"] == "t-audit-1"
    assert entry["operation"] == "jira.comment.add"
    assert entry["target"] == "PROJ-42"
    assert entry["durationMs"] == 123
    assert entry["input"]["commentBody"] == "test comment"
    assert entry["result"]["success"] is True


def test_jira_audit_log_skipped_when_no_workspace(tmp_path):
    """_write_jira_audit() must be a no-op when workspace_path is empty."""
    from jira.app import _write_jira_audit
    # Should not raise and should not write any file
    _write_jira_audit(
        workspace_path="",
        message={},
        operation="jira.comment.add",
        target="PROJ-1",
        input_summary={},
        result={},
    )


def main():
    tests = [
        fn for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  ✅ {test_fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  ❌ {test_fn.__name__}: {exc}")
            failed += 1

    print(f"\nPassed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
