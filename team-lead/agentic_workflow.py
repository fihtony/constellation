"""Team Lead-specific helpers for runtime-driven orchestration."""

from __future__ import annotations

import os
import threading

from common.prompt_builder import build_task_prompt
from common.runtime.adapter import summarize_runtime_configuration
from common.tools.control_tools import configure_control_tools

DEFAULT_TEAM_LEAD_SKILL_PLAYBOOKS = [
    "constellation-generic-agent-workflow",
    "constellation-architecture-delivery",
    "constellation-frontend-delivery",
    "constellation-backend-delivery",
    "constellation-database-delivery",
    "constellation-code-review-delivery",
    "constellation-testing-delivery",
    "constellation-ui-evidence-delivery",
]

TEAM_LEAD_INPUT_REQUIRED_PREAMBLE = (
    "The Team Lead Agent requires additional information before proceeding with your task.\n\n"
)

TEAM_LEAD_RUNTIME_TOOL_NAMES = [
    "jira_get_ticket",
    "jira_add_comment",
    "jira_search",
    "jira_transition",
    "jira_assign",
    "jira_get_transitions",
    "jira_get_myself",
    "jira_create_issue",
    "jira_update_fields",
    "jira_validate_permissions",
    "design_fetch_figma_screen",
    "design_fetch_stitch_screen",
    "scm_repo_inspect",
    "scm_read_file",
    "scm_list_dir",
    "scm_search_code",
    "scm_compare_refs",
    "scm_get_default_branch",
    "scm_get_pr_details",
    "scm_get_pr_diff",
    "dispatch_agent_task",
    "wait_for_agent_task",
    "ack_agent_task",
    "launch_per_task_agent",
    "complete_current_task",
    "fail_current_task",
    "request_user_input",
    "report_progress",
    "registry_query",
    "list_available_agents",
    "check_agent_status",
    "get_task_context",
    "get_agent_runtime_status",
    "todo_write",
    "load_skill",
    "list_skills",
    "read_local_file",
    "write_local_file",
    "edit_local_file",
    "list_local_dir",
    "search_local_files",
    "read_file",
    "write_file",
    "glob",
    "grep",
    "run_validation_command",
    "collect_task_evidence",
    "check_definition_of_done",
]


def build_team_lead_runtime_config(skill_playbooks=None):
    return {
        "runtime": summarize_runtime_configuration(),
        "skillPlaybooks": list(skill_playbooks or DEFAULT_TEAM_LEAD_SKILL_PLAYBOOKS),
    }


def make_wait_for_user_input(
    *,
    task_id: str,
    callback_url: str,
    task_store,
    input_events: dict,
    input_events_lock,
    notify_compass,
    input_wait_timeout: int,
    input_required_preamble: str,
):
    def _wait_for_user_input(question: str) -> str | None:
        task_store.update_state(task_id, "TASK_STATE_INPUT_REQUIRED", question)
        input_event = threading.Event()
        with input_events_lock:
            input_events[task_id] = {"event": input_event, "info": None}
        notify_compass(
            callback_url,
            task_id,
            "TASK_STATE_INPUT_REQUIRED",
            input_required_preamble + question,
        )
        if not input_event.wait(timeout=input_wait_timeout):
            with input_events_lock:
                input_events.pop(task_id, None)
            return None
        with input_events_lock:
            entry = input_events.pop(task_id, {})
        user_reply = entry.get("info") or ""
        task_store.update_state(task_id, "TASK_STATE_WORKING", "Resumed with user input")
        return user_reply

    return _wait_for_user_input


def configure_team_lead_control_tools(
    *,
    task_id: str,
    agent_id: str,
    workspace: str,
    permissions: dict | None,
    compass_task_id: str,
    callback_url: str,
    orchestrator_url: str,
    user_text: str,
    wait_for_input_fn,
):
    configure_control_tools(
        task_context={
            "taskId": task_id,
            "agentId": agent_id,
            "workspacePath": workspace,
            "permissions": permissions,
            "compassTaskId": compass_task_id,
            "callbackUrl": callback_url,
            "orchestratorUrl": orchestrator_url,
            "userText": user_text[:500],
        },
        complete_fn=lambda result, artifacts: None,
        fail_fn=lambda error: None,
        input_required_fn=lambda question, context: None,
        wait_for_input_fn=wait_for_input_fn,
    )


def build_team_lead_task_prompt(
    *,
    user_text: str,
    workspace: str,
    compass_task_id: str,
    team_lead_task_id: str,
    callback_url: str,
    max_review_cycles: int,
    stop_before_dev_dispatch: bool = False,
) -> str:
    validation_section = ""
    if stop_before_dev_dispatch:
        validation_section = """
## VALIDATION CHECKPOINT (stopBeforeDevDispatch=true)
IMPORTANT: Do NOT dispatch a development agent in this run.
Instead, complete steps 1-3 only (Analyze, Gather, Plan), then call `complete_current_task`
with `validationCheckpoint=true` in the metadata. Include your full analysis and plan in the summary.
"""
    agent_dir = os.path.dirname(__file__)
    template = build_task_prompt(agent_dir, "orchestrate")
    if not template:
        raise RuntimeError(
            "Missing team lead task prompt template: team-lead/prompts/tasks/orchestrate.md"
        )
    return template.format(
        validation_section=validation_section,
        user_text=user_text,
        workspace=workspace,
        compass_task_id=compass_task_id,
        team_lead_task_id=team_lead_task_id,
        callback_url=callback_url,
        max_review_cycles=max_review_cycles,
    )