#!/usr/bin/env python3
"""Focused regression tests for instance reporter registry recovery."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from common.instance_reporter import InstanceReporter


def test_heartbeat_reregisters_instance_after_registry_restart():
    reporter = InstanceReporter(
        agent_id="jira-agent",
        service_url="http://jira:8010",
        port=8010,
        registry_url="http://registry:9000",
        enabled=False,
    )
    reporter.instance_id = "stale-instance"

    with patch("common.instance_reporter._post_json") as post_json:
        post_json.side_effect = [
            {"ok": False, "status": 404, "body": None},
            {"ok": True, "status": 200, "body": {"instance_id": "new-instance"}},
        ]

        reporter._heartbeat_once()

    assert reporter.instance_id == "new-instance"


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