from __future__ import annotations

import json
import os
import tempfile
import unittest

from common.devlog import record_workspace_stage


class DevlogTests(unittest.TestCase):
    def test_stage_summary_no_longer_tracks_phases_array(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_workspace_stage(
                temp_dir,
                "web-agent",
                "Analyzing request",
                task_id="task-1234",
                extra={"runtimeConfig": {"runtime": {"requestedBackend": "copilot-cli"}}},
            )
            record_workspace_stage(
                temp_dir,
                "web-agent",
                "Completed request",
                task_id="task-1234",
                extra={"statusText": "ok"},
            )

            summary_path = os.path.join(temp_dir, "web-agent", "stage-summary.json")
            with open(summary_path, encoding="utf-8") as handle:
                payload = json.load(handle)

        self.assertEqual(payload["taskId"], "task-1234")
        self.assertEqual(payload["currentPhase"], "Completed request")
        self.assertNotIn("phases", payload)
        self.assertNotIn("phasesLog", payload)
        self.assertEqual(payload["runtimeConfig"]["runtime"]["requestedBackend"], "copilot-cli")

    def test_command_log_prefixes_cross_agent_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_workspace_stage(
                temp_dir,
                "compass",
                "Plan ready — 3 file(s) to implement",
                task_id="task-1234",
                extra={"sourceAgent": "web-agent"},
            )

            log_path = os.path.join(temp_dir, "compass", "command-log.txt")
            with open(log_path, encoding="utf-8") as handle:
                entry = handle.read().strip()

        self.assertIn("[Web Agent]", entry)
        self.assertTrue(entry.endswith("Plan ready — 3 file(s) to implement"))


if __name__ == "__main__":
    unittest.main()