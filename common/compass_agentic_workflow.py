"""Compass-specific helpers for runtime-driven workflow execution."""

from __future__ import annotations

import json
import os

from common.agent_system_prompt import build_agent_system_prompt
from common.message_utils import extract_text
from common.runtime.adapter import get_runtime
from common.tools.control_tools import configure_control_tools

COMPASS_RUNTIME_TOOL_NAMES = [
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
    "aggregate_task_card",
    "derive_user_facing_status",
    "load_skill",
    "list_skills",
    "todo_write",
    "read_local_file",
    "list_local_dir",
    "search_local_files",
    "read_file",
    "glob",
    "grep",
]


def build_compass_workflow_prompt(
    *,
    user_text: str,
    workflow: list[str],
    workspace_path: str,
    task_id: str,
    advertised_url: str,
    compass_instance_id: str,
    max_revisions: int,
) -> str:
    return (
        f"Execute this multi-agent workflow for the user.\n\n"
        f"## User Request\n{user_text}\n\n"
        f"## Planned Workflow Steps\n"
        f"{json.dumps(workflow, ensure_ascii=False)}\n\n"
        f"## Instructions\n"
        f"1. Use discovery tools first if you need to verify agent availability or workspace evidence.\n"
        f"2. For each downstream capability you decide to run, use `dispatch_agent_task`, then `wait_for_agent_task`.\n"
        f"   - If dispatch fails because no idle instance is available, use `launch_per_task_agent` first.\n"
        f"3. If a downstream agent reports INPUT_REQUIRED, forward the question to the user via `request_user_input`.\n"
        f"4. After the final step completes, verify completeness using `aggregate_task_card`:\n"
        f"   - Pass the artifacts from the last callback to `aggregate_task_card`.\n"
        f"   - If isComplete=false, inspect completenessIssues and dispatch a follow-up (max {max_revisions} retries).\n"
        f"   - Use `derive_user_facing_status` to determine the correct status label for the user.\n"
        f"5. Prefer shared-workspace aliases (`read_local_file`, `list_local_dir`, `search_local_files`) when inspecting evidence.\n"
        f"6. When all steps are complete, call `complete_current_task` with a user-friendly summary.\n"
        f"7. If any step fails irreversibly, call `fail_current_task` with a clear explanation.\n"
        f"8. Use `ack_agent_task` after you are done with each per-task agent.\n\n"
        f"## Metadata to pass downstream\n"
        f"- sharedWorkspacePath: {workspace_path}\n"
        f"- orchestratorTaskId: {task_id}\n"
        f"- orchestratorCallbackUrl: {advertised_url}/tasks/{task_id}/callbacks?instance={compass_instance_id}\n"
        f"- permissions: available through `get_task_context`; pass them downstream unchanged\n"
    )


def run_compass_workflow(
    *,
    task_id,
    task,
    message,
    workflow,
    agent_id: str,
    agent_file: str,
    route_system_prompt: str,
    advertised_url: str,
    compass_instance_id: str,
    max_revisions: int,
    timeout_seconds: int,
    get_task,
    update_state_and_notify,
    summarize_for_user,
    add_progress_step,
    audit_log,
):
    metadata = message.get("metadata") or {}
    user_text = extract_text(message)

    def _on_complete(result_text, artifacts):
        final_message = summarize_for_user(task, "TASK_STATE_COMPLETED", result_text, artifacts, workflow)
        update_state_and_notify(task_id, "TASK_STATE_COMPLETED", final_message)
        audit_log("TASK_COMPLETED", task_id=task_id, final_state="TASK_STATE_COMPLETED")

    def _on_fail(error_message):
        update_state_and_notify(task_id, "TASK_STATE_FAILED", error_message)
        audit_log("TASK_FAILED", task_id=task_id, error=error_message)

    def _on_input_required(question, _context):
        update_state_and_notify(task_id, "TASK_STATE_INPUT_REQUIRED", question)
        add_progress_step(task_id, question, agent_id=agent_id)

    configure_control_tools(
        task_context={
            "taskId": task_id,
            "agentId": agent_id,
            "workspacePath": task.workspace_path or "",
            "workflow": workflow,
            "permissions": metadata.get("permissions"),
            "userText": user_text[:500],
        },
        complete_fn=_on_complete,
        fail_fn=_on_fail,
        input_required_fn=_on_input_required,
    )

    task_prompt = build_compass_workflow_prompt(
        user_text=user_text,
        workflow=list(workflow or []),
        workspace_path=task.workspace_path or "",
        task_id=task_id,
        advertised_url=advertised_url,
        compass_instance_id=compass_instance_id,
        max_revisions=max_revisions,
    )
    system_prompt = build_agent_system_prompt(agent_file, route_system_prompt)

    update_state_and_notify(task_id, "TASK_STATE_WORKING", f"Executing workflow: {', '.join(workflow)}")

    try:
        result = get_runtime().run_agentic(
            task=task_prompt,
            system_prompt=system_prompt,
            cwd=task.workspace_path or os.getcwd(),
            tools=COMPASS_RUNTIME_TOOL_NAMES,
            max_turns=80,
            timeout=timeout_seconds,
        )

        current_task = get_task(task_id)
        if current_task and current_task.state not in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
            if result.success:
                summary = result.summary or "Workflow completed."
                update_state_and_notify(task_id, "TASK_STATE_COMPLETED", summary)
                audit_log("TASK_COMPLETED", task_id=task_id, final_state="TASK_STATE_COMPLETED")
            else:
                error = result.summary or "Workflow failed — runtime did not complete successfully."
                update_state_and_notify(task_id, "TASK_STATE_FAILED", error)
                audit_log("TASK_FAILED", task_id=task_id, error=error)
    except Exception as error:
        update_state_and_notify(task_id, "TASK_STATE_FAILED", f"Workflow error: {error}")
        audit_log("TASK_FAILED", task_id=task_id, error=str(error))