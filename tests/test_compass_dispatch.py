from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault(
    "ARTIFACT_ROOT",
    os.path.join(tempfile.gettempdir(), "constellation-test-artifacts"),
)

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
                    "summary": "Implement PROJ-42 with design context",
                    "jira_ticket_key": "PROJ-42",
                    "design_url": "https://www.figma.com/file/demo",
                    "design_type": "figma",
                },
            })
            self._write_json(workspace, "team-lead/design-context.json", {
                "url": "https://www.figma.com/file/demo",
                "type": "figma",
                "page_name": "Home Screen",
            })
            self._write_text(workspace, "compass/command-log.txt", "[12:00:01] Created task\n")
            self._write_text(workspace, "team-lead/command-log.txt", "[12:00:03] Reviewing implementation\n")

            task = TaskStore().create()
            task.workspace_path = workspace
            task.original_message = {"parts": [{"text": "Please implement PROJ-42 from Figma"}]}
            task.pending_workflow = ["team-lead.task.analyze"]
            task.state = "TASK_STATE_COMPLETED"
            task.status_message = "Implementation finished."
            task.progress_steps = [
                {"step": "Analyzing request", "agentId": "team-lead-agent", "ts": 1},
                {"step": "Reviewing implementation", "agentId": "team-lead-agent", "ts": 2},
            ]
            task.artifacts = [
                {
                    "name": "web-pr-evidence",
                    "artifactType": "text/plain",
                    "parts": [{"text": "PR evidence"}],
                    "metadata": {
                        "prUrl": "https://github.com/example/repo/pull/9",
                        "url": "https://github.com/example/repo/pull/9",
                        "branch": "feature/PROJ-42_task-0001",
                        "jiraInReview": True,
                    },
                }
            ]

            card = compass_app._serialize_task_card(task)

        self.assertEqual(card["summary"], "Implement PROJ-42 with design context")
        self.assertEqual(card["jiraTicketId"], "PROJ-42")
        self.assertEqual(card["design"]["url"], "https://www.figma.com/file/demo")
        self.assertEqual(card["design"]["pageName"], "Home Screen")
        self.assertEqual(card["statusLabel"], "Completed / In Review")
        self.assertEqual(card["currentMajorStep"], "Reviewing implementation")
        self.assertEqual(card["commandLogSections"][0]["agentId"], "team-lead")
        self.assertEqual(card["pr"]["url"], "https://github.com/example/repo/pull/9")

    def test_resume_input_required_task_reuses_same_compass_task(self):
        original_store = compass_app.task_store
        compass_app.task_store = TaskStore()
        try:
            task = compass_app.task_store.create()
            task.state = "TASK_STATE_INPUT_REQUIRED"
            task.status_message = "Please confirm the tech stack."
            task.downstream_task_id = "tl-task-7"
            task.downstream_service_url = "http://team-lead:8030"
            task.original_message = {"parts": [{"text": "Implement PROJ-2"}]}

            body = {
                "contextId": task.task_id,
                "message": {"parts": [{"text": "Use Python Flask."}]},
            }
            message = body["message"]

            with mock.patch.object(compass_app, "_a2a_call") as call_mock, mock.patch.object(
                compass_app,
                "audit_log",
            ):
                resumed = compass_app._resume_input_required_task(body, message)

            self.assertIsNotNone(resumed)
            self.assertEqual(resumed["id"], task.task_id)
            self.assertEqual(compass_app.task_store.get(task.task_id).state, "TASK_STATE_WORKING")
            self.assertEqual(compass_app.task_store.get(task.task_id).status_message, "User provided additional information. Resuming…")
            call_mock.assert_called_once_with(
                "http://team-lead:8030",
                {"parts": [{"text": "Use Python Flask."}]},
                context_id="tl-task-7",
            )
        finally:
            compass_app.task_store = original_store

    def test_route_with_runtime_can_choose_office_workflow(self):
        with mock.patch.object(compass_app, "_run_agentic", return_value=json.dumps({
            "summary": "Summarize the local sales workbook.",
            "workflow": ["office.data.analyze"],
            "task_type": "office",
            "office_subtype": "analyze",
            "target_paths": ["/Users/example/Documents/sales.xlsx"],
            "needs_input": False,
            "input_question": None,
            "reasoning": "Local spreadsheet analysis belongs to Office Agent.",
        })):
            decision = compass_app._route_with_runtime(
                "Please analyze /Users/example/Documents/sales.xlsx and summarize the trends."
            )

        self.assertEqual(decision["workflow"], ["office.data.analyze"])
        self.assertEqual(decision["task_type"], "office")
        self.assertEqual(decision["office_subtype"], "analyze")
        self.assertEqual(decision["target_paths"], ["/Users/example/Documents/sales.xlsx"])
        self.assertFalse(decision["needs_input"])

    def test_summarize_for_user_prefers_runtime_summary(self):
        task = TaskStore().create()
        task.original_message = {"parts": [{"text": "Summarize the generated report."}]}

        with mock.patch.object(compass_app, "_run_agentic", return_value=json.dumps({
            "summary": "Completed. The generated report is ready for review.",
            "highlights": ["report generated"],
            "warnings": [],
        })):
            summary = compass_app._summarize_for_user(
                task,
                "TASK_STATE_COMPLETED",
                "Workflow completed.",
                [{"name": "final-summary", "text": "Report created in workspace."}],
                ["team-lead.task.analyze"],
            )

        self.assertEqual(summary, "Completed. The generated report is ready for review.")

    def test_build_step_message_includes_exit_rule(self):
        task = TaskStore().create()
        task.workspace_path = "/tmp/workspace"
        message = {"parts": [{"text": "Implement the task."}], "metadata": {}}

        payload = compass_app._build_step_message(
            task,
            message,
            task.task_id,
            "team-lead.task.analyze",
            1,
            1,
            [],
        )

        self.assertEqual(payload["metadata"]["exitRule"]["type"], "wait_for_parent_ack")
        self.assertEqual(
            payload["metadata"]["exitRule"]["ack_timeout_seconds"],
            compass_app.COMPASS_CHILD_ACK_TIMEOUT,
        )

    def test_route_and_dispatch_office_task_prompts_for_output_mode(self):
        original_store = compass_app.task_store
        compass_app.task_store = TaskStore()
        try:
            with tempfile.TemporaryDirectory(prefix="compass_office_route_") as workspace, \
                 tempfile.NamedTemporaryFile(suffix=".csv") as handle, \
                 mock.patch.object(compass_app, "_create_shared_workspace", return_value=workspace), \
                 mock.patch.object(compass_app, "_route_with_runtime", return_value={
                     "summary": "Analyze the local CSV file.",
                     "workflow": ["office.data.analyze"],
                     "task_type": "office",
                     "office_subtype": "analyze",
                     "target_paths": [handle.name],
                     "needs_input": False,
                     "input_question": None,
                 }), \
                 mock.patch.object(compass_app, "audit_log"), \
                 mock.patch.object(compass_app, "record_workspace_stage"):
                task_dict = compass_app.route_and_dispatch(
                    {"parts": [{"text": f"Analyze {handle.name}"}]}
                )

            self.assertEqual(task_dict["status"]["state"], "TASK_STATE_INPUT_REQUIRED")
            self.assertIn("workspace only", task_dict["status"]["message"]["parts"][0]["text"])
            self.assertEqual(task_dict["routerContext"]["awaitingStep"], "output_mode")
            self.assertEqual(task_dict["routerContext"]["requestedCapability"], "office.data.analyze")
        finally:
            compass_app.task_store = original_store

    def test_route_and_dispatch_office_missing_path_prompts_for_clarification(self):
        original_store = compass_app.task_store
        compass_app.task_store = TaskStore()
        try:
            with tempfile.TemporaryDirectory(prefix="compass_office_clarify_") as workspace, \
                 mock.patch.object(compass_app, "_create_shared_workspace", return_value=workspace), \
                 mock.patch.object(compass_app, "_route_with_runtime", return_value={
                     "summary": "Summarize the local document.",
                     "workflow": ["office.document.summarize"],
                     "task_type": "office",
                     "office_subtype": "summarize",
                     "target_paths": [],
                     "needs_input": True,
                     "input_question": "Please provide the absolute path to the document.",
                 }), \
                 mock.patch.object(compass_app, "audit_log"), \
                 mock.patch.object(compass_app, "record_workspace_stage"):
                task_dict = compass_app.route_and_dispatch(
                    {"parts": [{"text": "Summarize my local report"}]}
                )

            self.assertEqual(task_dict["status"]["state"], "TASK_STATE_INPUT_REQUIRED")
            self.assertIn("absolute path", task_dict["status"]["message"]["parts"][0]["text"])
            self.assertEqual(task_dict["routerContext"]["awaitingStep"], "clarify_path")
            self.assertEqual(task_dict["routerContext"]["requestedCapability"], "office.document.summarize")
        finally:
            compass_app.task_store = original_store

    def test_validate_office_target_paths_rejects_relative_path(self):
        paths, error = compass_app._validate_office_target_paths(["docs/report.txt"])
        self.assertEqual(paths, [])
        self.assertIn("Path must be absolute", error)

    def test_validate_office_target_paths_defers_missing_host_path_when_containerized(self):
        host_path = "/Users/example/projects/constellation/tests/data/csv/sales_data.csv"
        # Simulate running inside a container by patching the helper directly.
        with mock.patch.object(compass_app, "_is_containerized", return_value=True), \
             mock.patch.object(compass_app.os.path, "exists", return_value=False):
            paths, error = compass_app._validate_office_target_paths([host_path])

        self.assertEqual(paths, [host_path])
        self.assertEqual(error, "")

        self.assertEqual(paths, [host_path])
        self.assertEqual(error, "")

    def test_validate_office_target_paths_rejects_outside_whitelist(self):
        with tempfile.TemporaryDirectory(prefix="compass_allow_") as allowed, tempfile.TemporaryDirectory(prefix="compass_other_") as other:
            outside = Path(other, "report.txt")
            outside.write_text("secret", encoding="utf-8")
            with mock.patch.object(compass_app, "OFFICE_ALLOWED_BASE_PATHS", [allowed]):
                paths, error = compass_app._validate_office_target_paths([str(outside)])
            self.assertEqual(paths, [])
            self.assertIn("outside OFFICE_ALLOWED_BASE_PATHS", error)

    def test_validate_office_target_paths_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory(prefix="compass_allow_") as allowed, tempfile.TemporaryDirectory(prefix="compass_other_") as other:
            outside = Path(other, "report.txt")
            outside.write_text("secret", encoding="utf-8")
            link = Path(allowed, "linked-report.txt")
            link.symlink_to(outside)
            with mock.patch.object(compass_app, "OFFICE_ALLOWED_BASE_PATHS", [allowed]):
                paths, error = compass_app._validate_office_target_paths([str(link)])
            self.assertEqual(paths, [])
            self.assertIn("outside OFFICE_ALLOWED_BASE_PATHS", error)

    def test_resume_input_required_office_workspace_reuses_same_task(self):
        original_store = compass_app.task_store
        compass_app.task_store = TaskStore()
        try:
            task = compass_app.task_store.create()
            task.state = "TASK_STATE_INPUT_REQUIRED"
            task.workspace_path = "/app/artifacts/workspaces/task-0001"
            task.status_message = "Choose output target"
            task.original_message = {"parts": [{"text": "Analyze the CSV file."}]}
            task.pending_workflow = ["office.data.analyze"]
            task.router_context = {
                "kind": "office",
                "awaitingStep": "output_mode",
                "requestedCapability": "office.data.analyze",
                "officeSubtype": "analyze",
                "targetPaths": ["/Users/example/Documents/sales.csv"],
            }

            def fake_start(current_task, message, workflow):
                compass_app.task_store.update_state(current_task.task_id, "ROUTING", "Planned workflow: office.data.analyze")
                return current_task.to_dict()

            with mock.patch.object(compass_app, "_interpret_office_reply", return_value={"action": "workspace", "clarification_question": None}), \
                 mock.patch.object(compass_app, "_start_task_worker", side_effect=fake_start), \
                 mock.patch.object(compass_app.launcher, "resolve_host_path", return_value="/tmp/artifacts-host/workspaces/task-0001"):
                resumed = compass_app._resume_input_required_task(
                    {"contextId": task.task_id},
                    {"parts": [{"text": "Use workspace output."}]},
                )

            self.assertEqual(resumed["id"], task.task_id)
            self.assertEqual(compass_app.task_store.get(task.task_id).state, "ROUTING")
            self.assertEqual(task.router_context["outputMode"], "workspace")
            self.assertIn("/Users/example/Documents", task.router_context["dispatch"]["mountRootHostPath"])
            self.assertIn(":ro", task.router_context["dispatch"]["extraBinds"][0])
        finally:
            compass_app.task_store = original_store

    def test_resume_input_required_office_inplace_requires_write_confirmation(self):
        original_store = compass_app.task_store
        compass_app.task_store = TaskStore()
        try:
            task = compass_app.task_store.create()
            task.state = "TASK_STATE_INPUT_REQUIRED"
            task.status_message = "Choose output target"
            task.original_message = {"parts": [{"text": "Organize the folder."}]}
            task.pending_workflow = ["office.folder.organize"]
            task.router_context = {
                "kind": "office",
                "awaitingStep": "output_mode",
                "requestedCapability": "office.folder.organize",
                "officeSubtype": "organize",
                "targetPaths": ["/Users/example/Documents/2026"],
            }

            with mock.patch.object(compass_app, "_interpret_office_reply", return_value={"action": "inplace", "clarification_question": None}):
                resumed = compass_app._resume_input_required_task(
                    {"contextId": task.task_id},
                    {"parts": [{"text": "Modify the original folder directly."}]},
                )

            self.assertEqual(resumed["id"], task.task_id)
            self.assertEqual(compass_app.task_store.get(task.task_id).state, "TASK_STATE_INPUT_REQUIRED")
            self.assertEqual(task.router_context["awaitingStep"], "confirm_write")
            self.assertIn("Approve write access", compass_app.task_store.get(task.task_id).status_message)
        finally:
            compass_app.task_store = original_store

    def test_resume_input_required_office_denied_write_falls_back_to_workspace(self):
        original_store = compass_app.task_store
        compass_app.task_store = TaskStore()
        try:
            task = compass_app.task_store.create()
            task.state = "TASK_STATE_INPUT_REQUIRED"
            task.status_message = "Approve write access"
            task.original_message = {"parts": [{"text": "Organize the folder."}]}
            task.pending_workflow = ["office.folder.organize"]
            task.router_context = {
                "kind": "office",
                "awaitingStep": "confirm_write",
                "requestedCapability": "office.folder.organize",
                "officeSubtype": "organize",
                "targetPaths": ["/Users/example/Documents/2026"],
                "outputMode": "inplace",
            }

            with mock.patch.object(compass_app, "_interpret_office_reply", return_value={"action": "deny", "clarification_question": None}), \
                 mock.patch.object(compass_app, "_start_task_worker", side_effect=lambda current_task, *_args, **_kwargs: current_task.to_dict()):
                resumed = compass_app._resume_input_required_task(
                    {"contextId": task.task_id},
                    {"parts": [{"text": "No, do not modify the original folder."}]},
                )

            self.assertEqual(resumed["id"], task.task_id)
            self.assertEqual(task.router_context["outputMode"], "workspace")
            self.assertEqual(compass_app.task_store.get(task.task_id).state, "TASK_STATE_INPUT_REQUIRED")
        finally:
            compass_app.task_store = original_store

    def test_route_with_runtime_recognizes_dev_task(self):
        """TC-PERM-08: LLM classifies a development request as 'dev' task."""
        with mock.patch.object(compass_app, "_run_agentic", return_value=json.dumps({
            "summary": "Develop a new iOS application.",
            "workflow": ["team-lead.task.analyze"],
            "task_type": "dev",
            "office_subtype": None,
            "target_paths": [],
            "needs_input": False,
            "input_question": None,
            "reasoning": "Building an iOS app is a software development task.",
        })):
            decision = compass_app._route_with_runtime(
                "Help me develop an iOS application with SwiftUI."
            )

        self.assertEqual(decision["task_type"], "dev")
        self.assertEqual(decision["workflow"], ["team-lead.task.analyze"])
        self.assertFalse(decision["needs_input"])

    def test_validate_office_target_paths_rejects_dotdot(self):
        """TC-PERM-05: Path containing .. resolves outside the whitelist is rejected."""
        with tempfile.TemporaryDirectory(prefix="compass_allow_") as allowed, \
             tempfile.TemporaryDirectory(prefix="compass_other_") as other:
            outside = Path(other, "secret.txt")
            outside.write_text("secret", encoding="utf-8")
            # The path uses .. to escape the allowed directory
            sneaky = os.path.join(allowed, "..", os.path.basename(other), "secret.txt")
            with mock.patch.object(compass_app, "OFFICE_ALLOWED_BASE_PATHS", [allowed]):
                paths, error = compass_app._validate_office_target_paths([sneaky])
            self.assertEqual(paths, [])
            self.assertIn("outside OFFICE_ALLOWED_BASE_PATHS", error)

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