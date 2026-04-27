from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from compass import app as compass_app
from common.task_store import TaskStore


class CompassDispatchTests(unittest.TestCase):
    def test_should_launch_fresh_instance_for_per_task_agents(self):
        self.assertTrue(
            compass_app._should_launch_fresh_instance({"execution_mode": "per-task"})
        )
        self.assertFalse(
            compass_app._should_launch_fresh_instance({"execution_mode": "persistent"})
        )

    def test_task_store_list_tasks_returns_newest_first(self):
        store = TaskStore()
        first = store.create()
        second = store.create()

        ordered = store.list_tasks()

        self.assertEqual([task.task_id for task in ordered], [second.task_id, first.task_id])

    def test_serialize_task_card_reads_workspace_metadata_and_command_logs(self):
        with tempfile.TemporaryDirectory(prefix="compass_card_") as workspace:
            self._write_json(workspace, "team-lead/stage-summary.json", {
                "currentPhase": "Reviewing implementation",
                "analysis": {
                    "summary": "Implement CSTL-42 with design context",
                    "jira_ticket_key": "CSTL-42",
                    "design_url": "https://www.figma.com/file/demo",
                    "design_type": "figma",
                },
            })
            self._write_json(workspace, "team-lead/design-context.json", {
                "url": "https://www.figma.com/file/demo",
                "type": "figma",
                "page_name": "Home Screen",
            })
            self._write_json(workspace, "web-agent/pr-evidence.json", {
                "url": "https://github.com/example/repo/pull/9",
                "branch": "feature/CSTL-42_task-0001",
            })
            self._write_json(workspace, "web-agent/jira-actions.json", {
                "events": [
                    {"action": "transition", "status": "completed", "targetStatus": "In Review"},
                ]
            })
            self._write_text(workspace, "compass/command-log.txt", "[12:00:01] Created task\n")
            self._write_text(workspace, "team-lead/command-log.txt", "[12:00:03] Reviewing implementation\n")

            task = TaskStore().create()
            task.workspace_path = workspace
            task.original_message = {"parts": [{"text": "Please implement CSTL-42 from Figma"}]}
            task.pending_workflow = ["team-lead.task.analyze"]
            task.state = "TASK_STATE_COMPLETED"
            task.status_message = "Implementation finished."
            task.progress_steps = [
                {"step": "Analyzing request", "agentId": "team-lead-agent", "ts": 1},
                {"step": "Reviewing implementation", "agentId": "team-lead-agent", "ts": 2},
            ]

            card = compass_app._serialize_task_card(task)

        self.assertEqual(card["summary"], "Implement CSTL-42 with design context")
        self.assertEqual(card["jiraTicketId"], "CSTL-42")
        self.assertEqual(card["design"]["url"], "https://www.figma.com/file/demo")
        self.assertEqual(card["design"]["pageName"], "Home Screen")
        self.assertEqual(card["statusLabel"], "Completed / In Review")
        self.assertEqual(card["currentMajorStep"], "Reviewing implementation")
        self.assertEqual(card["commandLogSections"][0]["agentId"], "team-lead")
        self.assertEqual(card["pr"]["url"], "https://github.com/example/repo/pull/9")

    def _write_json(self, workspace: str, relative_path: str, payload: dict) -> None:
        full_path = Path(workspace, relative_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_text(self, workspace: str, relative_path: str, content: str) -> None:
        full_path = Path(workspace, relative_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()