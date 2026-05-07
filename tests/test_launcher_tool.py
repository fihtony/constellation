#!/usr/bin/env python3
"""Tests for the per-task launcher tool Registry parsing behavior."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import common.tools.launcher_tool as _launcher  # noqa: F401
from common.tools.registry import get_tool


class LauncherToolTests(unittest.TestCase):
    def setUp(self):
        self.tool = get_tool("launch_per_task_agent")

    def test_lookup_agent_for_capability_accepts_registry_list_shape(self):
        payload = [
            {
                "agent_id": "office-agent",
                "execution_mode": "per-task",
                "launch_spec": {"image": "constellation-office-agent:latest"},
            }
        ]

        class _R:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(payload).encode()

        with patch("common.tools.launcher_tool.urlopen", return_value=_R()):
            info = self.tool._lookup_agent_for_capability("office.data.analyze")

        self.assertEqual(info["agent_id"], "office-agent")

    def test_wait_for_registration_accepts_instances_list_shape(self):
        payload = [{"instance_id": "inst-1", "service_url": "http://office-agent:8060", "status": "idle"}]

        class _R:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(payload).encode()

        with patch("common.tools.launcher_tool.urlopen", return_value=_R()), \
             patch("common.tools.launcher_tool.time.sleep"):
            instance = self.tool._wait_for_registration("office-agent", "office-agent-task-1")

        self.assertEqual(instance["instance_id"], "inst-1")
        self.assertEqual(instance["service_url"], "http://office-agent:8060")


if __name__ == "__main__":
    unittest.main()