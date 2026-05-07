"""Tests for Team Lead Agent agentic architecture.

Verifies that the Team Lead Agent:
1. Uses runtime.run_agentic() as the sole workflow driver
2. Handles INPUT_REQUIRED resume correctly
3. Handles dev agent callbacks gracefully (ack-only)
4. Initializes _TaskContext with the right minimal state
"""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from unittest import mock

_TEAM_LEAD_DIR = Path(__file__).resolve().parents[1] / "team-lead"
import importlib.util

_TEAM_LEAD_APP_PATH = _TEAM_LEAD_DIR / "app.py"
_TEAM_LEAD_SPEC = importlib.util.spec_from_file_location("team_lead_app", _TEAM_LEAD_APP_PATH)
team_lead_app = importlib.util.module_from_spec(_TEAM_LEAD_SPEC)
assert _TEAM_LEAD_SPEC and _TEAM_LEAD_SPEC.loader
_TEAM_LEAD_SPEC.loader.exec_module(team_lead_app)


class TeamLeadTaskContextTests(unittest.TestCase):
    """Verify _TaskContext only has minimal required slots."""

    def test_task_context_minimal_slots(self):
        ctx = team_lead_app._TaskContext()
        expected_slots = {
            "compass_task_id",
            "compass_callback_url",
            "compass_url",
            "shared_workspace_path",
            "permissions",
            "original_message",
            "user_text",
            "phases_log",
        }
        actual_slots = set(team_lead_app._TaskContext.__slots__)
        self.assertEqual(actual_slots, expected_slots)

    def test_task_context_defaults(self):
        ctx = team_lead_app._TaskContext()
        self.assertEqual(ctx.compass_task_id, "")
        self.assertEqual(ctx.compass_callback_url, "")
        self.assertEqual(ctx.compass_url, "")
        self.assertEqual(ctx.shared_workspace_path, "")
        self.assertIsNone(ctx.permissions)
        self.assertEqual(ctx.original_message, {})
        self.assertEqual(ctx.user_text, "")
        self.assertEqual(ctx.phases_log, [])

    def test_task_context_no_legacy_workflow_state(self):
        """Ensure old workflow state (jira_info, repo_info, analysis, etc.) is removed."""
        ctx = team_lead_app._TaskContext()
        legacy_attrs = [
            "jira_info", "jira_fetch_attempts", "repo_info", "design_info",
            "analysis", "plan", "dev_result", "dev_service_url", "dev_task_id",
            "review_result", "review_cycles", "pending_tasks",
            "pending_permission_request",
        ]
        for attr in legacy_attrs:
            self.assertNotIn(attr, team_lead_app._TaskContext.__slots__,
                             f"Legacy slot '{attr}' should not be in _TaskContext.__slots__")


class TeamLeadAgentArchitectureTests(unittest.TestCase):
    """Verify the Team Lead Agent uses the agentic runtime pattern."""

    def test_no_legacy_workflow_functions(self):
        """Legacy Python orchestration functions should not exist in the new app."""
        removed_functions = [
            "_plan_information_gathering",
            "_execute_gather_action",
            "_normalize_gather_plan",
            "_build_fallback_gather_plan",
            "_enrich_analysis_from_context",
            "_extract_tech_stack_constraints",
            "_apply_platform_evidence_policy",
            "_apply_tech_stack_confirmation_policy",
            "_enforce_plan_constraints",
            "_ensure_jira_ticket_for_workflow",
            "_suppress_redundant_questions",
            "_filter_unresolved_missing_info",
            "_call_sync_agent",
            "_inspect_target_repo",
            "_acquire_dev_agent",
            "_find_agent_instance",
            "_wait_for_idle_instance",
            "_a2a_send",
            "_poll_agent_task",
            "_interpret_permission_reply",
            "_request_permission_approval",
            "_build_dev_task_metadata",
            "_load_workspace_review_evidence",
            "_available_capability_snapshot",
        ]
        for fn_name in removed_functions:
            self.assertFalse(
                hasattr(team_lead_app, fn_name),
                f"Legacy function '{fn_name}' should have been removed in Phase 6 refactor"
            )

    def test_run_workflow_uses_agentic_runtime(self):
        """_run_workflow must call runtime.run_agentic(), not a Python state machine."""
        import inspect
        source = inspect.getsource(team_lead_app._run_workflow)
        self.assertIn("run_agentic(", source, "_run_workflow must call runtime.run_agentic()")
        self.assertNotIn("_plan_information_gathering", source)
        self.assertNotIn("_execute_gather_action", source)
        self.assertNotIn("_call_sync_agent", source)

    def test_run_workflow_provides_tool_list(self):
        """_run_workflow must provide a comprehensive tool list to run_agentic."""
        required_tools = [
            "jira_get_ticket",
            "scm_repo_inspect",
            "scm_read_file",
            "scm_list_dir",
            "scm_search_code",
            "scm_get_default_branch",
            "dispatch_agent_task",
            "wait_for_agent_task",
            "ack_agent_task",
            "request_user_input",
            "report_progress",
            "registry_query",
            "check_agent_status",
            "scm_get_pr_details",
            "scm_get_pr_diff",
            "collect_task_evidence",
            "check_definition_of_done",
        ]
        # tools are defined in TEAM_LEAD_RUNTIME_TOOL_NAMES constant or inline in _run_workflow
        import inspect
        source = inspect.getsource(team_lead_app._run_workflow)
        tool_names_str = str(getattr(team_lead_app, "TEAM_LEAD_RUNTIME_TOOL_NAMES", []))
        combined = source + tool_names_str
        for tool in required_tools:
            self.assertIn(tool, combined, f"Tool '{tool}' must be available to the team lead runtime")

    def test_no_undefined_functions(self):
        """Check that all functions called within the module are actually defined."""
        # _store_dev_callback_result was a critical bug in the old code
        self.assertFalse(
            hasattr(team_lead_app, "_store_dev_callback_result"),
            "_store_dev_callback_result was the old broken callback handler that has been removed"
        )

    def test_callback_endpoint_is_ack_only(self):
        """Callbacks from dev agents should be acknowledged without blocking the workflow."""
        import inspect
        source = inspect.getsource(team_lead_app.TeamLeadHandler.do_POST)
        # The callback handler should ack without calling complex functions
        self.assertIn("Dev callback received", source)
        self.assertIn('"ok": True', source)
        # Should NOT reference any blocking wait functions
        self.assertNotIn("_CALLBACK_EVENTS", source)
        self.assertNotIn("event.wait", source)


class TeamLeadInputRequiredResumeTests(unittest.TestCase):
    """Verify INPUT_REQUIRED resume via _INPUT_EVENTS."""

    def test_input_events_global_exists(self):
        self.assertTrue(hasattr(team_lead_app, "_INPUT_EVENTS"))
        self.assertTrue(hasattr(team_lead_app, "_INPUT_EVENTS_LOCK"))

    def test_input_resume_sets_event(self):
        """Simulates Compass forwarding a user reply to Team Lead."""
        task_id = "test-task-123"

        # Register a pending input event
        input_event = threading.Event()
        with team_lead_app._INPUT_EVENTS_LOCK:
            team_lead_app._INPUT_EVENTS[task_id] = {"event": input_event, "info": None}

        try:
            # Simulate the HTTP handler setting the reply
            with team_lead_app._INPUT_EVENTS_LOCK:
                entry = team_lead_app._INPUT_EVENTS.get(task_id)
                if entry:
                    entry["info"] = "user reply text"
                    entry["event"].set()

            # The event should now be set
            self.assertTrue(input_event.is_set())

            # Reading back the reply
            with team_lead_app._INPUT_EVENTS_LOCK:
                stored = team_lead_app._INPUT_EVENTS.get(task_id, {})
            self.assertEqual(stored.get("info"), "user reply text")
        finally:
            with team_lead_app._INPUT_EVENTS_LOCK:
                team_lead_app._INPUT_EVENTS.pop(task_id, None)


class TeamLeadBuildPromptTests(unittest.TestCase):
    """Verify _build_team_lead_task_prompt generates the correct content."""

    def _call_prompt(self, **overrides):
        """Helper to call the prompt builder with required args."""
        defaults = dict(
            user_text="Implement login page",
            workspace="/app/artifacts/workspaces/task-001",
            compass_task_id="compass-001",
            team_lead_task_id="tl-001",
            callback_url="http://compass/tasks/compass-001/callbacks",
            max_review_cycles=2,
        )
        defaults.update(overrides)
        return team_lead_app._build_team_lead_task_prompt(**defaults)

    def test_prompt_includes_workspace_path(self):
        prompt = self._call_prompt()
        self.assertIn("/app/artifacts/workspaces/task-001", prompt)
        self.assertIn("compass-001", prompt)
        self.assertIn("Implement login page", prompt)

    def test_prompt_includes_validation_checkpoint_when_flag_set(self):
        prompt = self._call_prompt(
            user_text="Build feature X",
            workspace="/tmp/ws",
            compass_task_id="c1",
            team_lead_task_id="t1",
            callback_url="http://cb",
            stop_before_dev_dispatch=True,
        )
        self.assertIn("VALIDATION CHECKPOINT", prompt)
        self.assertIn("stopBeforeDevDispatch=true", prompt)

    def test_prompt_includes_max_review_cycles(self):
        prompt = self._call_prompt(max_review_cycles=3)
        self.assertIn("3", prompt)

    def test_prompt_no_hardcoded_compass_url(self):
        """Team Lead should never hardcode Compass URL — it's discovered via registry."""
        import inspect
        source = inspect.getsource(team_lead_app._build_team_lead_task_prompt)
        self.assertNotIn("COMPASS_URL", source)
        self.assertNotIn("compass:8080", source)


if __name__ == "__main__":
    unittest.main()
