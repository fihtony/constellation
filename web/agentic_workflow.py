"""Web Agent-specific helpers for runtime-driven execution.

Mirrors the agent-local workflow module pattern used by Team Lead and Android:
implementation specialist that clones repos, writes code, validates, and
creates PRs — all driven by the agentic runtime via tools.
"""

from __future__ import annotations

import os

from common.runtime.adapter import summarize_runtime_configuration
from common.tools.control_tools import configure_control_tools

# ---------------------------------------------------------------------------
# Tool names exposed to the agentic runtime backend
# ---------------------------------------------------------------------------

WEB_AGENT_RUNTIME_TOOL_NAMES = [
    # --- Control lifecycle ---
    "complete_current_task",
    "fail_current_task",
    "request_user_input",
    "request_agent_clarification",
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
    # --- Jira boundary tools (use handed-off context first; write updates via A2A) ---
    "jira_validate_permissions",
    "jira_get_myself",
    "jira_get_transitions",
    "jira_assign",
    "jira_transition",
    "jira_add_comment",
    # --- SCM boundary tools (via SCM Agent) ---
    "scm_clone_repo",
    "scm_get_default_branch",
    "scm_get_branch_rules",
    "scm_list_branches",
    "scm_create_branch",
    "scm_push_files",
    "scm_create_pr",
    "scm_get_pr_details",
    "scm_get_pr_diff",
    "scm_read_file",
    "scm_list_dir",
    "scm_search_code",
    "scm_repo_inspect",
    # --- Validation and evidence ---
    "run_validation_command",
    "collect_task_evidence",
    "check_definition_of_done",
    "summarize_failure_context",
    # --- Design context (supplemental, when Team Lead context is truncated) ---
    "design_fetch_figma_screen",
    "design_fetch_stitch_screen",
]

# Default skill playbooks loaded into the web agent system prompt.
DEFAULT_WEB_AGENT_SKILL_PLAYBOOKS = [
    "constellation-generic-agent-workflow",
    "constellation-architecture-delivery",
    "constellation-frontend-delivery",
    "constellation-backend-delivery",
    "constellation-database-delivery",
    "constellation-code-review-delivery",
    "constellation-testing-delivery",
    "constellation-ui-evidence-delivery",
]


# ---------------------------------------------------------------------------
# Runtime config summary
# ---------------------------------------------------------------------------

def build_web_agent_runtime_config(skill_playbooks=None) -> dict:
    """Return runtime config dict for stage-summary.json."""
    return {
        "runtime": summarize_runtime_configuration(),
        "skillPlaybooks": list(skill_playbooks or DEFAULT_WEB_AGENT_SKILL_PLAYBOOKS),
    }


# ---------------------------------------------------------------------------
# Tool wiring
# ---------------------------------------------------------------------------

def configure_web_agent_control_tools(
    *,
    task_id: str,
    agent_id: str,
    workspace: str,
    permissions: dict | None,
    compass_task_id: str,
    callback_url: str,
    orchestrator_url: str,
    user_text: str,
    wait_for_input_fn=None,
) -> None:
    """Wire lifecycle callbacks into common control_tools for this task.

    Called by app.py before run_agentic().  The complete_fn and fail_fn are
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
        wait_for_input_fn=wait_for_input_fn,
    )


# ---------------------------------------------------------------------------
# Task prompt builder
# ---------------------------------------------------------------------------

def build_web_task_prompt(
    *,
    user_text: str,
    workspace: str,
    compass_task_id: str,
    web_task_id: str,
    acceptance_criteria: list,
    is_revision: bool,
    review_issues: list,
    tech_stack_constraints: dict,
    design_context: dict,
    target_repo_url: str,
    repo_workspace_path: str,
    jira_context: str,
    ticket_key: str,
    permissions: dict | None,
) -> str:
    """Build the task prompt forwarded to runtime.run_agentic()."""
    import json

    from common.prompt_builder import build_task_prompt

    # Normalise sections
    criteria_text = (
        "\n".join(f"- {c}" for c in acceptance_criteria)
        if acceptance_criteria
        else "Not specified."
    )
    issues_text = (
        "\n".join(f"- {i}" for i in review_issues) if review_issues else ""
    )
    tech_text = (
        json.dumps(tech_stack_constraints, ensure_ascii=False)
        if tech_stack_constraints
        else "None"
    )
    design_text = str(design_context.get("content") or "") if design_context else ""
    design_url = str(design_context.get("url") or "") if design_context else ""

    revision_section = ""
    if is_revision and issues_text:
        revision_section = (
            f"\n## REVISION REQUEST\n"
            "This is a revision. Fix the following issues from the previous implementation:\n"
            f"{issues_text}\n"
        )

    jira_section = ""
    if ticket_key and jira_context:
        jira_section = (
            f"## Jira Ticket Context ({ticket_key})\n{jira_context[:3000]}"
        )
    elif ticket_key:
        jira_section = f"## Jira Ticket\nKey: {ticket_key}"

    design_section = ""
    if design_url or design_text:
        design_section = (
            "## Design Context\n"
            f"URL: {design_url or '(see content below)'}\n"
            f"{design_text[:2000] if design_text else ''}"
        )

    task_template = build_task_prompt(
        os.path.join(os.path.dirname(__file__), "..", "web"), "implement"
    )
    if not task_template:
        raise RuntimeError(
            "Missing web agent task prompt template: web/prompts/tasks/implement.md"
        )

    return task_template.format(
        user_text=user_text,
        criteria_text=criteria_text,
        tech_text=tech_text,
        jira_section=jira_section or "Not provided.",
        design_section=design_section or "Not provided.",
        revision_section=revision_section.strip() or "Not a revision task.",
        target_repo_url=target_repo_url or "(detect from context)",
        repo_workspace_path=repo_workspace_path or "(must be provided by Team Lead for repo-backed tasks)",
        ticket_key=ticket_key or "none",
        workspace=workspace or "(no shared workspace provided)",
        compass_task_id=compass_task_id or "",
        web_task_id=web_task_id,
    )
