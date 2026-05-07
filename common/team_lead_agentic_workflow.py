"""Team Lead-specific helpers for runtime-driven orchestration."""

from __future__ import annotations

import threading

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
]

TEAM_LEAD_RUNTIME_TOOL_NAMES = [
    "jira_get_ticket",
    "jira_add_comment",
    "design_fetch_figma_screen",
    "design_fetch_stitch_screen",
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
    return f"""You are the Team Lead Agent. Execute this development task autonomously.
{validation_section}
## User Request
{user_text}

## Your Workflow (follow this order)

### 1. ANALYZE
- Identify the task type (implementation, bug fix, refactoring, etc.)
- Extract Jira ticket key if present (pattern: PROJ-123)
- Identify target platform and technology stack
- Use `report_progress` to announce "Analyzing request"

### 2. GATHER CONTEXT
- Use `jira_get_ticket` to fetch ticket details if a Jira key is found
- Use `design_fetch_figma_screen` or `design_fetch_stitch_screen` if design URLs are present
- Use `registry_query` to discover available agents and their capabilities
- Use `check_agent_status` to verify boundary agents are reachable before calling them
- If critical information is missing, use `request_user_input` to ask the user
- Use `report_progress` to announce "Gathering context"

### 3. PLAN
- Create an implementation plan with acceptance criteria
- Write the plan to the workspace using `write_local_file`
- Determine which development agent to use (android.task.execute, web.task.execute, etc.)
- Use `report_progress` to announce "Creating plan"

### 4. EXECUTE
- Use `launch_per_task_agent` if no idle development agent is available
- Use `dispatch_agent_task` to send the implementation task with full context in metadata:
  - jiraContext (from jira_get_ticket result), designContext, scmContext
  - sharedWorkspacePath: {workspace}
  - orchestratorTaskId: {compass_task_id}
  - permissions snapshot
  - exitRule: {{"type": "wait_for_parent_ack", "ack_timeout_seconds": 3600}}
- Use `wait_for_agent_task` to wait for the development agent to complete
- Use `report_progress` to announce "Executing implementation"

### 5. REVIEW
- Examine the dev agent's output artifacts and workspace files using `read_local_file`, `list_local_dir`, and `search_local_files`
- Check for PR URL and branch evidence in artifact metadata
- If output has issues, use `dispatch_agent_task` for a revision (max {max_review_cycles} cycles)
- Use `report_progress` to announce "Reviewing output"

### 6. COMPLETE
- Use `jira_add_comment` to post a completion comment if a Jira ticket exists
- Use `ack_agent_task` to acknowledge the dev agent (triggers graceful shutdown)
- Generate a final summary with PR URL, branch, and key results
- Use `complete_current_task` with the summary and PR evidence in artifacts metadata:
  - Include prUrl, branch, jiraInReview=true in artifacts metadata when PR is created
- Use `report_progress` to announce "Task completed"

## Task Metadata
- sharedWorkspacePath: {workspace}
- orchestratorTaskId: {compass_task_id}
- teamLeadTaskId: {team_lead_task_id}
- callbackUrl: {callback_url}

## Important Rules
- Never write product code yourself — always delegate to development agents
- Discover agents via `registry_query` or `list_available_agents`, never hardcode URLs
- If you cannot determine the platform or repo, ask the user via `request_user_input`
- Always ACK per-task agents after all review cycles are complete
- Include PR URL and branch in your final artifacts metadata (prUrl, branch fields)
- Set jiraInReview=true in final artifacts metadata when a PR is created
- Maximum review cycles: {max_review_cycles}
- All boundary agent calls must carry permissions through A2A metadata
"""