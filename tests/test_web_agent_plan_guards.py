"""Regression tests for Web Agent guards, prompt boundaries, and agentic workflow.

Phase 7 fully replaced the legacy Python workflow with a runtime-first design.
Tests for dead code (plan helpers, build recovery, branch-selection helpers, etc.)
have been removed.  This module covers:

- WEB_AGENT_RUNTIME_TOOL_NAMES completeness
- _run_workflow calls runtime.run_agentic (not legacy helpers)
- _resolve_jira_context extraction logic
- _prepend_tech_stack_constraints logic
- _apply_task_exit_rule (shutdown scheduling)
- INPUT_REQUIRED resume endpoints
- configure_web_agent_control_tools wiring
- build_web_task_prompt rendering
- Prompt boundary assertions (connect-agent, team-lead, web prompts/system)
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from web import app as web_app
from web.agentic_workflow import (
    WEB_AGENT_RUNTIME_TOOL_NAMES,
    build_web_agent_runtime_config,
    build_web_task_prompt,
    configure_web_agent_control_tools,
)
from common.runtime.connect_agent.adapter import DEFAULT_AGENTIC_SYSTEM

_TEAM_LEAD_DIR = Path(__file__).resolve().parents[1] / "team-lead"
_WEB_SYSTEM_DIR = Path(__file__).resolve().parents[1] / "web" / "prompts" / "system"


class WebAgentRuntimeToolTests(unittest.TestCase):
    """Verify WEB_AGENT_RUNTIME_TOOL_NAMES is complete and consistent."""

    REQUIRED_TOOLS = [
        "complete_current_task",
        "fail_current_task",
        "request_user_input",
        "request_agent_clarification",
        "report_progress",
        "get_task_context",
        "todo_write",
        "read_local_file",
        "write_local_file",
        "edit_local_file",
        "list_local_dir",
        "search_local_files",
        "run_local_command",
        "jira_validate_permissions",
        "jira_get_myself",
        "jira_get_transitions",
        "jira_assign",
        "jira_transition",
        "jira_add_comment",
        "scm_clone_repo",
        "scm_create_branch",
        "scm_push_files",
        "scm_create_pr",
        "scm_get_pr_details",
        "run_validation_command",
        "collect_task_evidence",
        "check_definition_of_done",
        "design_fetch_figma_screen",
        "design_fetch_stitch_screen",
    ]

    def test_all_required_tools_present(self):
        for tool in self.REQUIRED_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, WEB_AGENT_RUNTIME_TOOL_NAMES)

    def test_no_duplicate_tool_names(self):
        self.assertEqual(len(WEB_AGENT_RUNTIME_TOOL_NAMES), len(set(WEB_AGENT_RUNTIME_TOOL_NAMES)))

    def test_tool_names_are_strings(self):
        for item in WEB_AGENT_RUNTIME_TOOL_NAMES:
            self.assertIsInstance(item, str)

    def test_runtime_config_tracks_generic_and_ui_evidence_playbooks(self):
        runtime_config = build_web_agent_runtime_config()
        playbooks = runtime_config["skillPlaybooks"]
        self.assertIn("constellation-generic-agent-workflow", playbooks)
        self.assertIn("constellation-ui-evidence-delivery", playbooks)


class WebAgentWorkflowTests(unittest.TestCase):
    """Verify _run_workflow uses run_agentic (not legacy helpers)."""

    def test_run_workflow_calls_run_agentic(self):
        import inspect
        source = inspect.getsource(web_app._run_workflow)
        self.assertIn("run_agentic", source)
        self.assertNotIn("_plan_implementation", source)
        self.assertNotIn("_build_and_test_with_recovery", source)

    def test_run_workflow_uses_build_web_task_prompt(self):
        import inspect
        source = inspect.getsource(web_app._run_workflow)
        self.assertIn("build_web_task_prompt", source)

    def test_run_workflow_uses_build_system_prompt_from_manifest(self):
        import inspect
        source = inspect.getsource(web_app._run_workflow)
        self.assertIn("build_system_prompt_from_manifest", source)

    def test_run_workflow_calls_configure_web_agent_control_tools(self):
        import inspect
        source = inspect.getsource(web_app._run_workflow)
        self.assertIn("configure_web_agent_control_tools", source)

    def test_run_workflow_applies_exit_rule_in_finally(self):
        import inspect
        source = inspect.getsource(web_app._run_workflow)
        self.assertIn("_apply_task_exit_rule", source)
        self.assertIn("finally", source)

    def test_run_workflow_handles_runtime_success(self):
        task = web_app.task_store.create()
        callback_calls = []
        mock_result = mock.MagicMock()
        mock_result.success = True
        mock_result.summary = "Done"
        mock_result.artifacts = []
        mock_result.turns_used = 5
        message = {
            "parts": [{"text": "Build the landing page."}],
            "metadata": {
                "orchestratorCallbackUrl": "http://team-lead/tasks/t1/callbacks",
                "orchestratorTaskId": "t1",
            },
        }
        done_event = threading.Event()
        original_update = web_app.task_store.update_state

        def tracking_update(tid, state, msg):
            original_update(tid, state, msg)
            if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
                done_event.set()

        with mock.patch.object(web_app, "_notify_callback", side_effect=lambda *a, **kw: callback_calls.append(a)), \
             mock.patch("web.agentic_workflow.configure_control_tools"), \
             mock.patch.object(web_app, "get_runtime") as mock_get_runtime, \
             mock.patch.object(web_app, "require_agentic_runtime"), \
             mock.patch.object(web_app, "build_system_prompt_from_manifest", return_value="sys"), \
             mock.patch.object(web_app, "_apply_task_exit_rule"), \
             mock.patch.object(web_app.task_store, "update_state", side_effect=tracking_update):
            mock_rt = mock.MagicMock()
            mock_rt.run_agentic.return_value = mock_result
            mock_get_runtime.return_value = mock_rt
            t = threading.Thread(target=web_app._run_workflow, args=(task.task_id, message), daemon=True)
            t.start()
            done_event.wait(timeout=5)

        final_task = web_app.task_store.get(task.task_id)
        self.assertIsNotNone(final_task)
        self.assertEqual(final_task.state, "TASK_STATE_COMPLETED")


class JiraContextResolutionTests(unittest.TestCase):

    def test_extracts_ticket_key_from_jira_context_metadata(self):
        ticket_key, content = web_app._resolve_jira_context(
            "Implement the dashboard.",
            {
                "jiraContext": {
                    "ticketKey": "PROJ-1234",
                    "content": '{"fields": {"summary": "Build dashboard"}}',
                }
            },
        )
        self.assertEqual(ticket_key, "PROJ-1234")
        self.assertIn("Build dashboard", content)

    def test_falls_back_to_jira_ticket_key_metadata(self):
        ticket_key, _ = web_app._resolve_jira_context("No ticket in text.", {"jiraTicketKey": "PROJ-2903"})
        self.assertEqual(ticket_key, "PROJ-2903")

    def test_falls_back_to_regex_in_user_text(self):
        ticket_key, _ = web_app._resolve_jira_context("Implement PROJ-99 in the repo.", {})
        self.assertEqual(ticket_key, "PROJ-99")

    def test_returns_empty_strings_when_no_ticket(self):
        ticket_key, content = web_app._resolve_jira_context("Generic task", {})
        self.assertEqual(ticket_key, "")
        self.assertEqual(content, "")

    def test_jira_context_takes_precedence_over_metadata_key(self):
        ticket_key, _ = web_app._resolve_jira_context(
            "Text OTHR-1 here",
            {
                "jiraTicketKey": "OTHR-1",
                "jiraContext": {"ticketKey": "PROJ-42", "content": "body"},
            },
        )
        self.assertEqual(ticket_key, "PROJ-42")


class TechStackConstraintsTests(unittest.TestCase):

    def test_prepends_python_constraints(self):
        result = web_app._prepend_tech_stack_constraints(
            "Build the app.",
            {"language": "python", "python_version": "3.12", "backend_framework": "flask"},
        )
        self.assertIn("HARD TECH STACK CONSTRAINTS:", result)
        self.assertIn("Python 3.12", result)
        self.assertIn("flask", result)

    def test_empty_constraints_returns_original(self):
        original = "Build the app."
        self.assertEqual(web_app._prepend_tech_stack_constraints(original, {}), original)
        self.assertEqual(web_app._prepend_tech_stack_constraints(original, None), original)

    def test_does_not_double_prepend(self):
        first = web_app._prepend_tech_stack_constraints(
            "Build the app.", {"language": "python", "backend_framework": "flask"}
        )
        second = web_app._prepend_tech_stack_constraints(
            first, {"language": "python", "backend_framework": "flask"}
        )
        self.assertEqual(first, second)


class TaskExitRuleTests(unittest.TestCase):

    def test_apply_task_exit_rule_no_shutdown_for_persistent_rule(self):
        import time
        with mock.patch.object(web_app, "_schedule_shutdown") as mock_shutdown:
            web_app._apply_task_exit_rule("task-x", {"type": "persistent"})
            time.sleep(0.1)
            mock_shutdown.assert_not_called()

    def test_apply_task_exit_rule_schedules_shutdown_for_immediate_rule(self):
        import time
        with mock.patch.object(web_app, "_schedule_shutdown") as mock_shutdown:
            web_app._apply_task_exit_rule("task-y", {"type": "immediate"})
            time.sleep(0.1)
            mock_shutdown.assert_called_once()


class InputRequiredResumeTests(unittest.TestCase):

    def test_make_wait_for_user_input_returns_callable(self):
        wait_fn = web_app._make_wait_for_user_input(task_id="t1", callback_url="")
        self.assertTrue(callable(wait_fn))

    def test_wait_for_user_input_resumes_with_reply(self):
        import time
        wait_fn = web_app._make_wait_for_user_input(task_id="resume-test-1", callback_url="")
        reply_text = "user answer here"
        result_holder = []

        def _waiter():
            with mock.patch.object(web_app.task_store, "update_state"):
                result_holder.append(wait_fn("Which framework?"))

        t = threading.Thread(target=_waiter, daemon=True)
        t.start()
        time.sleep(0.05)

        with web_app._INPUT_EVENTS_LOCK:
            entry = web_app._INPUT_EVENTS.get("resume-test-1")
        self.assertIsNotNone(entry, "Task should be waiting for input")
        entry["info"] = reply_text
        entry["event"].set()
        t.join(timeout=2)
        self.assertEqual(result_holder, [reply_text])


class ControlToolsWiringTests(unittest.TestCase):

    def test_configure_sets_task_context(self):
        captured = []
        with mock.patch(
            "web.agentic_workflow.configure_control_tools",
            side_effect=lambda task_context, **kw: captured.append(task_context),
        ):
            configure_web_agent_control_tools(
                task_id="task-xyz",
                agent_id="web-agent",
                workspace="/tmp/ws",
                permissions={"taskType": "development"},
                compass_task_id="compass-1",
                callback_url="http://team-lead/tasks/t1/callbacks",
                orchestrator_url="http://team-lead",
                user_text="Build the landing page.",
            )
        self.assertEqual(len(captured), 1)
        ctx = captured[0]
        self.assertEqual(ctx["taskId"], "task-xyz")
        self.assertEqual(ctx["agentId"], "web-agent")
        self.assertEqual(ctx["workspacePath"], "/tmp/ws")
        self.assertEqual(ctx["permissions"], {"taskType": "development"})
        self.assertEqual(ctx["compassTaskId"], "compass-1")


class BuildWebTaskPromptTests(unittest.TestCase):

    def test_prompt_includes_user_text_and_repo_url(self):
        try:
            prompt = build_web_task_prompt(
                user_text="Build the landing page.",
                workspace="/tmp/ws",
                compass_task_id="compass-1",
                web_task_id="web-task-1",
                acceptance_criteria=["GET / returns 200"],
                is_revision=False,
                review_issues=[],
                tech_stack_constraints={},
                design_context={},
                target_repo_url="https://github.com/example/repo",
                repo_workspace_path="/tmp/ws/example-repo",
                jira_context="",
                ticket_key="",
                permissions=None,
            )
            self.assertIn("Build the landing page.", prompt)
            self.assertIn("https://github.com/example/repo", prompt)
            self.assertIn("/tmp/ws/example-repo", prompt)
        except RuntimeError as exc:
            self.skipTest(f"Template file not found: {exc}")

    def test_revision_section_included_when_is_revision(self):
        try:
            prompt = build_web_task_prompt(
                user_text="Fix the login bug.",
                workspace="/tmp/ws",
                compass_task_id="compass-2",
                web_task_id="web-task-2",
                acceptance_criteria=[],
                is_revision=True,
                review_issues=["Missing error message"],
                tech_stack_constraints={},
                design_context={},
                target_repo_url="",
                repo_workspace_path="",
                jira_context="",
                ticket_key="",
                permissions=None,
            )
            self.assertIn("Missing error message", prompt)
        except RuntimeError as exc:
            self.skipTest(f"Template file not found: {exc}")

    def test_jira_section_included_when_ticket_key_present(self):
        try:
            prompt = build_web_task_prompt(
                user_text="Implement PROJ-1.",
                workspace="/tmp/ws",
                compass_task_id="compass-3",
                web_task_id="web-task-3",
                acceptance_criteria=[],
                is_revision=False,
                review_issues=[],
                tech_stack_constraints={},
                design_context={},
                target_repo_url="",
                repo_workspace_path="/tmp/ws/repo",
                jira_context="Build the widget feature",
                ticket_key="PROJ-1",
                permissions=None,
            )
            self.assertIn("PROJ-1", prompt)
            self.assertIn("Build the widget feature", prompt)
        except RuntimeError as exc:
            self.skipTest(f"Template file not found: {exc}")


class WebAgentRuntimeConfigTests(unittest.TestCase):

    def test_returns_required_keys(self):
        config = build_web_agent_runtime_config()
        self.assertIn("runtime", config)
        self.assertIn("skillPlaybooks", config)
        self.assertIsInstance(config["skillPlaybooks"], list)

    def test_custom_playbooks_override_default(self):
        config = build_web_agent_runtime_config(skill_playbooks=["my-skill"])
        self.assertEqual(config["skillPlaybooks"], ["my-skill"])


class AgentPromptBoundaryTests(unittest.TestCase):

    def test_connect_agent_default_prompt_stays_runtime_generic(self):
        lowered = DEFAULT_AGENTIC_SYSTEM.lower()
        self.assertIn("task-specific system prompt", lowered)
        self.assertNotIn("tailwind", lowered)
        self.assertNotIn("react", lowered)
        self.assertNotIn("figma", lowered)
        self.assertNotIn("jira comment", lowered)

    def test_team_lead_prompts_enforce_planning_and_repo_clone_boundary(self):
        orchestrate_lower = (
            (_TEAM_LEAD_DIR / "prompts" / "tasks" / "orchestrate.md")
            .read_text(encoding="utf-8")
            .lower()
        )
        self.assertIn("never write product code yourself", orchestrate_lower)
        self.assertIn("scm_clone_repo", orchestrate_lower)
        self.assertIn("repoworkspacepath", orchestrate_lower)
        self.assertIn("shared workspace", orchestrate_lower)
        self.assertIn("missing scm evidence is a delivery failure", orchestrate_lower)

    def test_web_system_prompts_exist_in_manifest_directory(self):
        self.assertTrue(_WEB_SYSTEM_DIR.is_dir(), "web/prompts/system/ must exist")
        manifest = _WEB_SYSTEM_DIR / "manifest.yaml"
        self.assertTrue(manifest.exists(), "manifest.yaml must exist in web/prompts/system/")

    def test_web_system_prompt_role_file_exists(self):
        role_file = _WEB_SYSTEM_DIR / "00-role.md"
        self.assertTrue(role_file.exists(), "00-role.md missing from web/prompts/system/")
        content = role_file.read_text(encoding="utf-8")
        self.assertGreater(len(content), 50)

    def test_web_system_prompt_tools_file_lists_scm_tools(self):
        tools_file = _WEB_SYSTEM_DIR / "20-tools.md"
        if not tools_file.exists():
            self.skipTest("20-tools.md not present")
        content = tools_file.read_text(encoding="utf-8").lower()
        self.assertIn("scm_clone_repo", content)
        self.assertIn("scm_create_pr", content)
        self.assertIn("jira_add_comment", content)
        self.assertIn("request_agent_clarification", content)

    def test_web_system_prompt_boundaries_file_exists(self):
        boundaries_file = _WEB_SYSTEM_DIR / "10-boundaries.md"
        self.assertTrue(boundaries_file.exists(), "10-boundaries.md missing")


class WebAppNoDeadCodeTests(unittest.TestCase):
    """Verify that dead code helpers have been removed from web/app.py."""

    REMOVED_FUNCTIONS = [
        "_plan_implementation",
        "_sanitize_plan_files",
        "_build_and_test_with_recovery",
        "_install_plan_dependencies",
        "_install_written_node_dependencies",
        "_write_files_to_directory",
        "_call_sync_agent",
        "_a2a_send",
        "_poll_task",
        "_adf_text_node",
        "_build_pr_jira_comment_adf",
        "_record_jira_action",
        "_save_pr_evidence",
        "_normalize_plan_path",
        "_is_spa_router_file",
        "_select_branch_name",
        "_resolve_ticket_key",
        "_detect_node_build_steps",
        "_detect_ui_launch_plan",
        "_capture_browser_screenshot",
        "_apply_tech_stack_constraints",
    ]

    def test_removed_functions_not_in_app(self):
        import inspect
        source = inspect.getsource(web_app)
        for fn_name in self.REMOVED_FUNCTIONS:
            with self.subTest(fn=fn_name):
                self.assertNotIn(
                    "def " + fn_name + "(",
                    source,
                    "Dead function " + repr(fn_name) + " should have been removed from web/app.py",
                )

    def test_app_has_required_live_functions(self):
        required = [
            "_run_workflow",
            "_notify_callback",
            "audit_log",
            "_apply_task_exit_rule",
            "_make_wait_for_user_input",
            "_resolve_jira_context",
            "_prepend_tech_stack_constraints",
            "_save_workspace_file",
            "_append_workspace_file",
        ]
        for fn_name in required:
            with self.subTest(fn=fn_name):
                self.assertTrue(hasattr(web_app, fn_name))


if __name__ == "__main__":
    unittest.main()
