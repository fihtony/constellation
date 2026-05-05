from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from common.task_permissions import (
    build_permission_denied_artifact,
    build_permission_denied_details,
    grant_permission,
    load_permission_grant,
)

_TEAM_LEAD_DIR = Path(__file__).resolve().parents[1] / "team-lead"
_TEAM_LEAD_PROMPTS_SPEC = importlib.util.spec_from_file_location("team_lead.prompts", _TEAM_LEAD_DIR / "prompts.py")
team_lead_prompts = importlib.util.module_from_spec(_TEAM_LEAD_PROMPTS_SPEC)
assert _TEAM_LEAD_PROMPTS_SPEC and _TEAM_LEAD_PROMPTS_SPEC.loader
_TEAM_LEAD_PROMPTS_SPEC.loader.exec_module(team_lead_prompts)

team_lead_package = types.ModuleType("team_lead")
team_lead_package.__path__ = [str(_TEAM_LEAD_DIR)]
team_lead_package.prompts = team_lead_prompts
sys.modules.setdefault("team_lead", team_lead_package)
sys.modules.setdefault("team_lead.prompts", team_lead_prompts)

_TEAM_LEAD_APP_PATH = _TEAM_LEAD_DIR / "app.py"
_TEAM_LEAD_SPEC = importlib.util.spec_from_file_location("team_lead_app", _TEAM_LEAD_APP_PATH)
team_lead_app = importlib.util.module_from_spec(_TEAM_LEAD_SPEC)
assert _TEAM_LEAD_SPEC and _TEAM_LEAD_SPEC.loader
_TEAM_LEAD_SPEC.loader.exec_module(team_lead_app)


class TeamLeadPermissionEscalationTests(unittest.TestCase):
    def _failed_permission_task(self) -> dict:
        details = build_permission_denied_details(
            permission_agent="jira",
            target_agent="jira-agent",
            action="issue.update.description",
            target="PROJ-123",
            reason="Operation 'issue.update.description' is denied for agent 'jira' by task permissions.",
            task_id="jira-task-1",
            orchestrator_task_id="compass-task-1",
        )
        return {
            "id": "jira-task-1",
            "status": {
                "state": "TASK_STATE_FAILED",
                "message": {"parts": [{"text": details.reason}]},
            },
            "artifacts": [build_permission_denied_artifact(details, agent_id="jira-agent")],
        }

    def test_call_sync_agent_retries_after_permission_approval(self):
        ctx = team_lead_app._TaskContext()
        ctx.permissions = load_permission_grant("development").to_dict()

        def approve(*_args, **_kwargs):
            ctx.permissions = grant_permission(
                ctx.permissions,
                agent="jira",
                action="issue.update.description",
                scope="*",
                description="Approved by user",
            )
            return True

        completed_task = {
            "id": "jira-task-2",
            "status": {"state": "TASK_STATE_COMPLETED", "message": {"parts": [{"text": "done"}]}},
            "artifacts": [],
        }

        with mock.patch.object(
            team_lead_app.agent_directory,
            "resolve_capability",
            return_value=({"agent_id": "jira-agent"}, {"service_url": "http://jira:8010"}),
        ), mock.patch.object(
            team_lead_app,
            "_a2a_send",
            side_effect=[self._failed_permission_task(), completed_task],
        ) as mock_send, mock.patch.object(
            team_lead_app,
            "_request_permission_approval",
            side_effect=approve,
        ) as mock_approval:
            result = team_lead_app._call_sync_agent(
                "jira.ticket.update",
                "Update ticket PROJ-123 description",
                "team-task-1",
                "/tmp/workspace",
                "compass-task-1",
                permissions=ctx.permissions,
                ctx=ctx,
                callback_url="http://compass:8080/tasks/team-task-1/callbacks",
            )

        self.assertEqual(result["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(mock_approval.call_count, 1)

    def test_call_sync_agent_raises_when_user_denies_permission_request(self):
        ctx = team_lead_app._TaskContext()
        ctx.permissions = load_permission_grant("development").to_dict()

        with mock.patch.object(
            team_lead_app.agent_directory,
            "resolve_capability",
            return_value=({"agent_id": "jira-agent"}, {"service_url": "http://jira:8010"}),
        ), mock.patch.object(
            team_lead_app,
            "_a2a_send",
            return_value=self._failed_permission_task(),
        ), mock.patch.object(
            team_lead_app,
            "_request_permission_approval",
            return_value=False,
        ):
            with self.assertRaises(RuntimeError) as ctx_error:
                team_lead_app._call_sync_agent(
                    "jira.ticket.update",
                    "Update ticket PROJ-123 description",
                    "team-task-1",
                    "/tmp/workspace",
                    "compass-task-1",
                    permissions=ctx.permissions,
                    ctx=ctx,
                    callback_url="http://compass:8080/tasks/team-task-1/callbacks",
                )

        self.assertIn("User denied permission request", str(ctx_error.exception))


if __name__ == "__main__":
    unittest.main()
