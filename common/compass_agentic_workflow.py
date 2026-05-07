"""Compass-specific helpers for runtime-driven workflow execution."""

from __future__ import annotations

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
    "validate_office_paths",
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

# Path to the task prompt template file, relative to the compass agent directory.
_ORCHESTRATE_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "compass", "prompts", "tasks", "orchestrate.md"
)


def _load_orchestrate_template() -> str:
    """Load the orchestration task prompt template from the compass prompts directory."""
    path = os.path.normpath(_ORCHESTRATE_TEMPLATE_PATH)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def build_compass_workflow_prompt(
    *,
    user_text: str,
    workspace_path: str,
    task_id: str,
    advertised_url: str,
    compass_instance_id: str,
    max_revisions: int,
) -> str:
    template = _load_orchestrate_template()
    return template.format(
        user_text=user_text,
        workspace_path=workspace_path,
        task_id=task_id,
        advertised_url=advertised_url,
        compass_instance_id=compass_instance_id,
        max_revisions=max_revisions,
    )


def run_compass_workflow(
    *,
    task_id,
    task,
    message,
    agent_id: str,
    agent_file: str,
    advertised_url: str,
    compass_instance_id: str,
    max_revisions: int,
    timeout_seconds: int,
    get_task,
    update_state_and_notify,
    add_progress_step,
    audit_log,
):
    metadata = message.get("metadata") or {}
    user_text = extract_text(message)

    def _on_complete(result_text, artifacts):
        update_state_and_notify(task_id, "TASK_STATE_COMPLETED", result_text)
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
            "permissions": metadata.get("permissions"),
            "userText": user_text[:500],
        },
        complete_fn=_on_complete,
        fail_fn=_on_fail,
        input_required_fn=_on_input_required,
    )

    task_prompt = build_compass_workflow_prompt(
        user_text=user_text,
        workspace_path=task.workspace_path or "",
        task_id=task_id,
        advertised_url=advertised_url,
        compass_instance_id=compass_instance_id,
        max_revisions=max_revisions,
    )
    system_prompt = build_agent_system_prompt(agent_file)

    update_state_and_notify(task_id, "TASK_STATE_WORKING", "Processing request…")

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