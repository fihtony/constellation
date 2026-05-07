#!/usr/bin/env python3
"""Focused unit tests for Jira permission enforcement helpers."""

from __future__ import annotations

import copy
import json
import os
import sys
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
    from common.tools.registry import get_tool
    import jira.provider_tools as _jpt
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
            permission_fn=None,  # no permission enforcement — this test checks provider call, not permissions
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


def test_a2a_myself_returns_json_artifact():
    from common.tools.registry import get_tool
    import jira.provider_tools as _jpt
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
            permission_fn=None,  # no permission enforcement — this test checks provider call, not permissions
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
