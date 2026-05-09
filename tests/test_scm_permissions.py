#!/usr/bin/env python3
"""Focused unit tests for SCM permission enforcement helpers."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from common.task_permissions import load_permission_grant
# Ensure common scm_tools are registered before scm.provider_tools to avoid
# test-order-dependent tool registration conflicts in combined test runs.
import common.tools.scm_tools  # noqa: F401
from scm import app as scm_app


class _FakeHandler:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}
        self.sent_code: int | None = None
        self.sent_body: dict | None = None

    def _send_json(self, code: int, body: dict):
        self.sent_code = code
        self.sent_body = body


_DEVELOPMENT_PERMISSIONS = load_permission_grant("development").to_dict()


def test_branch_create_denied_without_permissions():
    handler = _FakeHandler()
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = scm_app._enforce_http_scm_permission(
            handler,
            action="branch.create",
            target="example/repo:feature/test-1",
            scope="feature/test-1",
        )
    assert not allowed
    assert handler.sent_code == 403


def test_repo_inspect_denied_without_permissions():
    handler = _FakeHandler()
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = scm_app._enforce_http_scm_permission(
            handler,
            action="repo.inspect",
            target="example/repo",
        )
    assert not allowed
    assert handler.sent_code == 403


def test_repo_inspect_allowed_with_development_grant():
    handler = _FakeHandler()
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = scm_app._enforce_http_scm_permission(
            handler,
            action="repo.inspect",
            target="example/repo",
            payload_permissions=_DEVELOPMENT_PERMISSIONS,
        )
    assert allowed
    assert handler.sent_code is None


def test_branch_create_allowed_for_dev_branch():
    handler = _FakeHandler()
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = scm_app._enforce_http_scm_permission(
            handler,
            action="branch.create",
            target="example/repo:agent/test-1",
            scope="agent/test-1",
            payload_permissions=_DEVELOPMENT_PERMISSIONS,
        )
    assert allowed
    assert handler.sent_code is None


def test_push_to_main_denied_by_scope():
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        try:
            scm_app._require_scm_permission(
                action="branch.push",
                target="example/repo:main",
                scope="main",
                payload_permissions=_DEVELOPMENT_PERMISSIONS,
            )
        except PermissionError as exc:
            assert "denied" in str(exc).lower() or "not in the allowed list" in str(exc)
        else:
            raise AssertionError("Expected PermissionError for push to main")


def test_pr_comment_allowed_with_development_grant():
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        scm_app._require_scm_permission(
            action="pr.comment",
            target="example/repo#12",
            scope="self",
            payload_permissions=_DEVELOPMENT_PERMISSIONS,
        )


def test_repo_tree_allowed_with_development_grant():
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        scm_app._require_scm_permission(
            action="repo.tree",
            target="/tmp/workspace/repo",
            payload_permissions=_DEVELOPMENT_PERMISSIONS,
        )


def test_invalid_permission_header_is_denied():
    handler = _FakeHandler(headers={"X-Task-Permissions": "{not-json}"})
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = scm_app._enforce_http_scm_permission(
            handler,
            action="repo.clone",
            target="example/repo",
        )
    assert not allowed
    assert handler.sent_code == 403
    assert "Invalid X-Task-Permissions header" in (handler.sent_body or {}).get("reason", "")


def test_extract_owner_repo_supports_bitbucket_personal_browse_url():
    owner, repo = scm_app._extract_owner_repo(
        "Clone repository https://bitbucket.example.com/users/user1/repos/web-ui-test/browse"
    )

    assert owner == "~user1"
    assert repo == "web-ui-test"


def test_parse_owner_repo_prefers_bitbucket_personal_browse_url_over_fallback_pairs():
    owner, repo = scm_app._parse_owner_repo(
        "Inspect repository https://bitbucket.example.com/users/user1/repos/web-ui-test/browse "
        "for the current task"
    )

    assert owner == "~user1"
    assert repo == "web-ui-test"


def test_extract_owner_repo_supports_bitbucket_personal_clone_url():
    owner, repo = scm_app._extract_owner_repo(
        "Clone repository https://bitbucket.example.com/scm/~user1/web-ui-test.git"
    )

    assert owner == "~user1"
    assert repo == "web-ui-test"


def test_parse_owner_repo_prefers_bitbucket_personal_clone_url_over_fallback_pairs():
    owner, repo = scm_app._parse_owner_repo(
        "Clone repository https://bitbucket.example.com/scm/~user1/web-ui-test.git "
        "to /app/artifacts/workspaces/task-0008"
    )

    assert owner == "~user1"
    assert repo == "web-ui-test"


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
