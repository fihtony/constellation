"""Team Lead Agent workflow nodes.

Each node is an async function that receives the workflow state dict and returns
a dict of state updates.
"""
from __future__ import annotations

import json
from typing import Any


async def receive_task(state: dict) -> dict:
    """Parse and validate the incoming task request."""
    return {
        "task_received": True,
        "jira_key": state.get("jira_key", ""),
        "repo_url": state.get("repo_url", ""),
    }


async def analyze_requirements(state: dict) -> dict:
    """Analyze the incoming task using LLM + context."""
    runtime = state.get("_runtime")
    user_request = state.get("user_request", "")

    if not runtime:
        return {
            "task_type": "general",
            "complexity": "medium",
            "required_skills": [],
            "analysis_summary": user_request,
        }

    from agents.team_lead.prompts.analysis import ANALYSIS_SYSTEM, ANALYSIS_TEMPLATE

    prompt = ANALYSIS_TEMPLATE.format(
        user_request=user_request,
        jira_key=state.get("jira_key", "N/A"),
    )
    result = await runtime.run(
        prompt=prompt,
        system_prompt=ANALYSIS_SYSTEM,
        max_tokens=2048,
    )

    raw = result.get("raw_response", "")
    try:
        analysis = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        analysis = {
            "task_type": "general",
            "complexity": "medium",
            "skills": [],
            "summary": raw or user_request,
        }

    return {
        "task_type": analysis.get("task_type", "general"),
        "complexity": analysis.get("complexity", "medium"),
        "required_skills": analysis.get("skills", []),
        "analysis_summary": analysis.get("summary", user_request),
    }


async def gather_context(state: dict) -> dict:
    """Gather Jira ticket + design context via boundary agents.

    In MVP this is a placeholder.  Full implementation uses A2A client to
    dispatch to Jira and UI Design agents.
    """
    jira_context = {}
    design_context = None

    # TODO: dispatch to jira.ticket.fetch via A2A client
    if state.get("jira_key"):
        jira_context = {"key": state["jira_key"], "status": "pending"}

    # TODO: dispatch to figma.page.fetch via A2A client
    if state.get("figma_url"):
        design_context = {"url": state["figma_url"], "status": "pending"}

    return {
        "jira_context": jira_context,
        "design_context": design_context,
    }


async def create_plan(state: dict) -> dict:
    """Create a development plan based on analysis and context."""
    runtime = state.get("_runtime")

    if not runtime:
        return {
            "plan": {
                "steps": [
                    {"step": 1, "action": "Clone repository"},
                    {"step": 2, "action": "Implement changes"},
                    {"step": 3, "action": "Run tests"},
                    {"step": 4, "action": "Create PR"},
                ],
            },
        }

    from agents.team_lead.prompts.planning import PLANNING_SYSTEM, PLANNING_TEMPLATE

    prompt = PLANNING_TEMPLATE.format(
        analysis=state.get("analysis_summary", ""),
        jira_context=json.dumps(state.get("jira_context", {}), ensure_ascii=False),
        task_type=state.get("task_type", "general"),
        complexity=state.get("complexity", "medium"),
    )
    result = await runtime.run(
        prompt=prompt,
        system_prompt=PLANNING_SYSTEM,
        max_tokens=2048,
    )

    raw = result.get("raw_response", "")
    try:
        plan = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        plan = {"steps": [{"step": 1, "action": raw or "Execute task"}]}

    return {"plan": plan}


async def select_skills(state: dict) -> dict:
    """Select relevant skills for the dev agent."""
    skills_registry = state.get("_skills_registry")
    required = state.get("required_skills", [])

    skill_context = ""
    if skills_registry and required:
        skill_context = skills_registry.build_prompt_context(required)

    return {"skill_context": skill_context}


async def dispatch_dev_agent(state: dict) -> dict:
    """Dispatch task to a dev agent (Web Dev, Android, etc.).

    MVP placeholder — full implementation dispatches via A2A to a per-task agent.
    """
    # TODO: use A2A client to dispatch to web.task.execute
    return {
        "dev_dispatched": True,
        "dev_result": state.get("dev_result", {}),
    }


async def wait_for_dev(state: dict) -> dict:
    """Wait for dev agent to complete.

    Sets route to 'completed', 'needs_clarification', or 'failed'.
    """
    dev_result = state.get("dev_result", {})
    dev_state = dev_result.get("state", "")

    if dev_state == "TASK_STATE_COMPLETED":
        return {"route": "completed", "pr_url": dev_result.get("pr_url", "")}
    if dev_state == "TASK_STATE_INPUT_REQUIRED":
        return {"route": "needs_clarification"}

    # Default: failed
    return {"route": "failed", "escalation_reason": "Dev agent did not complete."}


async def handle_question(state: dict) -> dict:
    """Handle a clarification question from the dev agent.

    Tries to answer it from context; escalates to user if unable.
    """
    # MVP: always escalate
    return {"route": "user_needed", "escalation_reason": "Clarification needed from user."}


async def dispatch_code_review(state: dict) -> dict:
    """Dispatch to the Code Review Agent.

    MVP placeholder — full implementation dispatches via A2A.
    """
    # TODO: dispatch to review.code.check via A2A
    return {
        "review_result": {
            "verdict": "approved",
            "comments": [],
        },
        "review_verdict": "approved",
        "review_comments": [],
    }


async def evaluate_review(state: dict) -> dict:
    """Evaluate code review result and decide next action."""
    review_cycles = state.get("review_cycles", 0) + 1
    max_cycles = state.get("max_review_cycles", 2)
    verdict = state.get("review_verdict", "rejected")

    if verdict == "approved":
        return {"route": "approved", "review_cycles": review_cycles}
    if review_cycles >= max_cycles:
        return {
            "route": "max_revisions",
            "review_cycles": review_cycles,
            "escalation_reason": f"Max review cycles ({max_cycles}) reached.",
        }
    return {
        "route": "needs_revision",
        "review_cycles": review_cycles,
        "revision_instructions": state.get("review_comments", []),
    }


async def request_revision(state: dict) -> dict:
    """Send revision request back to the dev agent."""
    return {
        "revision_requested": True,
        "review_feedback": state.get("review_comments", []),
    }


async def report_success(state: dict) -> dict:
    """Report successful task completion."""
    pr_url = state.get("pr_url", "N/A")
    return {
        "success": True,
        "summary": f"Task completed successfully. PR: {pr_url}",
    }


async def escalate_to_user(state: dict) -> dict:
    """Escalate to user for input or report failure."""
    from framework.workflow import interrupt

    reason = state.get("escalation_reason", "Unknown reason")
    interrupt(question=f"Unable to proceed: {reason}", escalation_type="user_input")
    # unreachable — interrupt raises
    return {}
