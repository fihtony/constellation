"""Office Agent-specific helpers for runtime-driven execution.

Mirrors the agent-local workflow module pattern used by other execution agents:
local document processing (summarize, analyze, organize) driven entirely by
the agentic runtime backend via tools.

Python code here handles only: tool wiring, task prompt assembly, and
runtime config. All workflow decisions belong to the LLM via tools.
"""

from __future__ import annotations

import os

from common.runtime.adapter import summarize_runtime_configuration
from common.tools.control_tools import configure_control_tools

# ---------------------------------------------------------------------------
# Tool names exposed to the agentic runtime backend
# ---------------------------------------------------------------------------

OFFICE_AGENT_RUNTIME_TOOL_NAMES = [
    # --- Control lifecycle ---
    "complete_current_task",
    "fail_current_task",
    "request_user_input",
    "report_progress",
    "get_task_context",
    "get_agent_runtime_status",
    # --- Planning ---
    "todo_write",
    # --- Skill discovery ---
    "load_skill",
    "list_skills",
    # --- Registry / agent status ---
    "registry_query",
    "check_agent_status",
    # --- Local workspace (canonical names) ---
    "read_local_file",
    "write_local_file",
    "edit_local_file",
    "list_local_dir",
    "search_local_files",
    "run_local_command",
    # --- Local workspace (legacy aliases — kept for backend compat) ---
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "bash",
    # --- Validation and evidence ---
    "collect_task_evidence",
    "check_definition_of_done",
    "summarize_failure_context",
]

# Default skill playbooks loaded into the office agent system prompt.
DEFAULT_OFFICE_AGENT_SKILL_PLAYBOOKS = [
    "constellation-generic-agent-workflow",
    "office-agent-workflow",
]


# ---------------------------------------------------------------------------
# Runtime config summary
# ---------------------------------------------------------------------------

def build_office_agent_runtime_config(skill_playbooks=None) -> dict:
    """Return runtime config dict for stage-summary.json."""
    return {
        "runtime": summarize_runtime_configuration(),
        "skillPlaybooks": list(skill_playbooks or DEFAULT_OFFICE_AGENT_SKILL_PLAYBOOKS),
    }


# ---------------------------------------------------------------------------
# Tool wiring
# ---------------------------------------------------------------------------

def configure_office_control_tools(
    *,
    task_id: str,
    agent_id: str,
    workspace: str,
    permissions: dict | None,
    compass_task_id: str,
    callback_url: str,
    orchestrator_url: str,
    user_text: str,
) -> None:
    """Wire lifecycle callbacks into common control_tools for this task.

    Called by app.py before run_agentic(). The complete_fn and fail_fn are
    left as no-ops here; app.py reads result.success from run_agentic() to
    drive the final task state update, which avoids double-write races.
    """
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
    )


# ---------------------------------------------------------------------------
# Task prompt builder
# ---------------------------------------------------------------------------

def build_office_task_prompt(
    *,
    user_text: str,
    capability: str,
    target_paths: list[str],
    output_mode: str,
    workspace_path: str,
    task_id: str,
    compass_task_id: str,
    agent_dir: str,
) -> str:
    """Build the task prompt forwarded to runtime.run_agentic().

    Loads the template from office/prompts/tasks/process.md and fills in
    task-specific context. All workflow logic stays in the LLM and skill.
    """
    from common.prompt_builder import build_task_prompt

    paths_text = (
        "\n".join(f"- {p}" for p in target_paths)
        if target_paths
        else "- (not specified — inspect the workspace for relevant files)"
    )

    if output_mode == "inplace":
        output_mode_section = (
            "IN-PLACE — write results directly back into the source files/directory."
        )
    elif workspace_path:
        output_mode_section = (
            f"WORKSPACE — write all output artifacts to: {workspace_path}/office-agent/"
        )
    else:
        output_mode_section = (
            "RETURN — return the summary as the task result artifact only."
        )

    task_template = build_task_prompt(agent_dir, "process")
    if not task_template:
        raise RuntimeError(
            "Missing office agent task prompt template: office/prompts/tasks/process.md"
        )

    return task_template.format(
        user_text=user_text,
        capability=capability,
        target_paths_text=paths_text,
        output_mode_section=output_mode_section,
        workspace_path=workspace_path or "(no shared workspace provided)",
        task_id=task_id,
        compass_task_id=compass_task_id or "",
    )
