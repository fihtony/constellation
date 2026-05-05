#!/usr/bin/env python3
"""Focused unit tests for UI Design permission enforcement helpers."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

_UI_DESIGN_DIR = os.path.join(_REPO_ROOT, "ui-design")
if _UI_DESIGN_DIR not in sys.path:
    sys.path.insert(0, _UI_DESIGN_DIR)

from common.task_permissions import load_permission_grant
import app as ui_app


class _FakeHandler:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}
        self.sent_code: int | None = None
        self.sent_body: dict | None = None

    def _send_json(self, code: int, body: dict):
        self.sent_code = code
        self.sent_body = body


_DEVELOPMENT_PERMISSIONS = load_permission_grant("development").to_dict()


def test_figma_read_denied_without_permissions():
    handler = _FakeHandler()
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = ui_app._enforce_http_ui_permission(
            handler,
            action="figma.read",
            target="https://www.figma.com/design/example",
        )
    assert not allowed
    assert handler.sent_code == 403


def test_figma_read_allowed_with_permissions():
    handler = _FakeHandler(headers={
        "X-Task-Permissions": json.dumps(_DEVELOPMENT_PERMISSIONS, ensure_ascii=False)
    })
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = ui_app._enforce_http_ui_permission(
            handler,
            action="figma.read",
            target="https://www.figma.com/design/example",
        )
    assert allowed
    assert handler.sent_code is None


def test_element_inspect_allowed_with_message_permissions():
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        ui_app._require_ui_permission(
            action="element.inspect",
            target="figma.node.get",
            message={"metadata": {"permissions": _DEVELOPMENT_PERMISSIONS}},
        )


def test_stitch_read_allowed_with_message_permissions():
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        ui_app._require_ui_permission(
            action="stitch.read",
            target="stitch.screen.fetch",
            message={"metadata": {"permissions": _DEVELOPMENT_PERMISSIONS}},
        )


def test_invalid_permission_header_is_denied():
    handler = _FakeHandler(headers={"X-Task-Permissions": "{not-json}"})
    with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
        allowed = ui_app._enforce_http_ui_permission(
            handler,
            action="stitch.read",
            target="stitch/tools",
        )
    assert not allowed
    assert handler.sent_code == 403
    assert "Invalid X-Task-Permissions header" in (handler.sent_body or {}).get("reason", "")


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
