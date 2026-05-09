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

    def test_route_and_dispatch_starts_worker_directly_without_prerouting(self):
        """route_and_dispatch() must create a task and start the worker without
        calling a single-shot routing LLM step — routing is the LLM's job inside run_agentic()."""
        original_store = compass_app.task_store
        compass_app.task_store = TaskStore()
        try:
            worker_calls = []

            def fake_worker(task, message, workflow):
                worker_calls.append({"task_id": task.task_id, "workflow": workflow})
                return task.to_dict()

            message = {"parts": [{"text": "Implement feature PROJ-42 from Figma."}]}
            with mock.patch.object(compass_app, "_start_task_worker", side_effect=fake_worker), \
                 mock.patch.object(compass_app, "require_agentic_runtime"), \
                 mock.patch.object(compass_app, "_create_shared_workspace", return_value="/tmp/ws"):
                result = compass_app.route_and_dispatch(message)

            self.assertEqual(len(worker_calls), 1)
            self.assertIn("task_id", worker_calls[0])
        finally:
            compass_app.task_store = original_store

    def test_route_and_dispatch_uses_explicit_capability_as_workflow_hint(self):
        """When a requested_capability is provided, it should be used as the workflow hint."""
        original_store = compass_app.task_store
        compass_app.task_store = TaskStore()
        try:
            worker_calls = []

            def fake_worker(task, message, workflow):
                worker_calls.append({"workflow": workflow})
                return task.to_dict()

            message = {"parts": [{"text": "Fetch Jira ticket ABC-1."}]}
            with mock.patch.object(compass_app, "_start_task_worker", side_effect=fake_worker), \
                 mock.patch.object(compass_app, "require_agentic_runtime"), \
                 mock.patch.object(compass_app, "_create_shared_workspace", return_value="/tmp/ws"):
                compass_app.route_and_dispatch(message, requested_capability="jira.ticket.fetch")

            self.assertEqual(worker_calls[0]["workflow"], ["jira.ticket.fetch"])
        finally:
            compass_app.task_store = original_store

    def test_route_and_dispatch_defaults_to_team_lead_hint(self):
        """Without an explicit capability, the default workflow hint is team-lead.task.analyze."""
        original_store = compass_app.task_store
        compass_app.task_store = TaskStore()
        try:
            worker_calls = []

            def fake_worker(task, message, workflow):
                worker_calls.append({"workflow": workflow})
                return task.to_dict()

            message = {"parts": [{"text": "Build a REST API for user management."}]}
            with mock.patch.object(compass_app, "_start_task_worker", side_effect=fake_worker), \
                 mock.patch.object(compass_app, "require_agentic_runtime"), \
                 mock.patch.object(compass_app, "_create_shared_workspace", return_value="/tmp/ws"):
                compass_app.route_and_dispatch(message)

            self.assertEqual(worker_calls[0]["workflow"], ["team-lead.task.analyze"])
        finally:
            compass_app.task_store = original_store

    def test_build_compass_workflow_prompt_loads_from_orchestrate_md(self):
        """build_compass_workflow_prompt() must load content from orchestrate.md, not hardcode it."""
        from compass.agentic_workflow import build_compass_workflow_prompt
        prompt = build_compass_workflow_prompt(
            user_text="Implement PROJ-10 feature",
            workspace_path="/tmp/ws/task-001",
            task_id="task-001",
            advertised_url="http://compass:8080",
            compass_instance_id="abc123",
            max_revisions=2,
        )
        # The prompt should include the user request
        self.assertIn("Implement PROJ-10 feature", prompt)
        # The prompt should have routing instructions (from orchestrate.md, not hardcoded)
        self.assertIn("team-lead.task.analyze", prompt)
        self.assertIn("dispatch_agent_task", prompt)
        self.assertIn("aggregate_task_card", prompt)
        self.assertIn("complete_current_task", prompt)

    def test_orchestrate_template_has_routing_decision_guidance(self):
        """orchestrate.md must contain routing classification guidance."""
        from compass.agentic_workflow import _load_orchestrate_template
        template = _load_orchestrate_template()
        self.assertIn("team-lead.task.analyze", template)
        self.assertIn("office.", template)
        self.assertIn("request_user_input", template)
        self.assertIn("validate_office_paths", template)
        self.assertIn("check_agent_status", template)
        self.assertIn("Do not use local filesystem tools", template)
        self.assertIn("do not ask the user to upload/copy the file", template)

    def test_run_compass_workflow_does_not_accept_workflow_parameter(self):
        """run_compass_workflow() must not have workflow or route_system_prompt params."""
        import inspect
        from compass.agentic_workflow import run_compass_workflow
        sig = inspect.signature(run_compass_workflow)
        self.assertNotIn("workflow", sig.parameters)
        self.assertNotIn("route_system_prompt", sig.parameters)
        self.assertNotIn("summarize_for_user", sig.parameters)

    def test_validate_office_target_paths_rejects_relative_path(self):
        paths, error = compass_app._validate_office_target_paths(["docs/report.txt"])
        self.assertEqual(paths, [])
        self.assertIn("Path must be absolute", error)

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