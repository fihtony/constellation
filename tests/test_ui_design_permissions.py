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
import importlib.util as _importlib_util
_ui_app_spec = _importlib_util.spec_from_file_location(
    "ui_design_app",
    os.path.join(_UI_DESIGN_DIR, "app.py"),
)
ui_app = _importlib_util.module_from_spec(_ui_app_spec)
_ui_app_spec.loader.exec_module(ui_app)


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


def test_stitch_fetch_image_tool_is_registered():
    """stitch_fetch_image must be in the global tool registry."""
    import importlib
    import sys
    # Ensure ui-design directory is importable as provider_tools
    _UI_DIR = os.path.join(_REPO_ROOT, "ui-design")
    if _UI_DIR not in sys.path:
        sys.path.insert(0, _UI_DIR)
    import provider_tools as _pt  # noqa: F401
    from common.tools.registry import get_tool
    tool = get_tool("stitch_fetch_image")
    assert tool is not None
    assert tool.schema.name == "stitch_fetch_image"


def test_ui_design_audit_log_written(tmp_path):
    """_write_ui_design_audit() must append a JSONL entry to the workspace audit file."""
    workspace = str(tmp_path)
    audit_path = tmp_path / "ui-design-agent" / "audit-log.jsonl"
    message = {
        "metadata": {
            "requestingAgent": "team-lead",
            "orchestratorTaskId": "t-100",
        }
    }

    ui_app._write_ui_design_audit(
        workspace_path=workspace,
        message=message,
        operation="figma.list_pages",
        target="file123",
        result={"success": True},
        duration_ms=42,
    )

    assert audit_path.exists(), "audit-log.jsonl was not created"
    import json
    lines = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["agentId"] == "ui-design-agent"
    assert entry["requestingTaskId"] == "t-100"
    assert entry["operation"] == "figma.list_pages"
    assert entry["target"] == "file123"
    assert entry["durationMs"] == 42
    assert entry["result"]["success"] is True


def test_ui_design_audit_log_appends_multiple(tmp_path):
    """Multiple audit calls must append to the same JSONL file."""
    workspace = str(tmp_path)
    for i in range(3):
        ui_app._write_ui_design_audit(
            workspace_path=workspace,
            message={"metadata": {"requestingAgent": "android-agent", "orchestratorTaskId": f"t-{i}"}},
            operation="stitch.list_screens",
            target="proj-abc",
            result={"success": True},
            duration_ms=10 * i,
        )
    audit_path = tmp_path / "ui-design-agent" / "audit-log.jsonl"
    import json
    lines = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 3
    assert lines[2]["requestingTaskId"] == "t-2"


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
