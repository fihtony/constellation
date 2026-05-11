"""Team Lead Agent workflow nodes.

Architecture: **Graph outside, ReAct inside**.

Each node is an async function that receives the workflow state dict and returns
a dict of state updates.  Nodes that need open-ended reasoning use the runtime
for single-shot LLM calls or bounded ReAct; the graph controls the macro flow.
"""
from __future__ import annotations

import json
import os
from typing import Any


async def receive_task(state: dict) -> dict:
    """Parse and validate the incoming task request."""
    user_request = state.get("user_request", "")
    return {
        "task_received": True,
        "jira_key": state.get("jira_key", ""),
        "repo_url": state.get("repo_url", ""),
        "figma_url": state.get("figma_url", ""),
        "revision_count": 0,
        "max_revisions": 3,
    }


async def analyze_requirements(state: dict) -> dict:
    """Analyze the incoming task using LLM (single-shot ReAct-inside-node)."""
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
    result = runtime.run(
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
    """Gather Jira ticket + design context via boundary agent tools.

    Uses the registered tools (fetch_jira_ticket, fetch_design) to call
    boundary agents via A2A dispatch.
    """
    from framework.tools.registry import get_registry

    registry = get_registry()
    jira_context = state.get("jira_context") or {}
    design_context = state.get("design_context")

    # Fetch Jira ticket if key provided and not already present
    jira_key = state.get("jira_key", "")
    if jira_key and not jira_context:
        try:
            result = registry.execute_sync("fetch_jira_ticket", ticket_key=jira_key)
            payload = json.loads(result.output) if result.output else {}
            if not payload.get("error"):
                jira_context = payload
        except Exception as exc:
            print(f"[team-lead] Jira fetch failed: {exc}")

    # Fetch design context if URL provided and not already present
    figma_url = state.get("figma_url", "")
    stitch_id = state.get("stitch_project_id", "")
    if (figma_url or stitch_id) and not design_context:
        try:
            kwargs: dict[str, str] = {}
            if figma_url:
                kwargs["figma_url"] = figma_url
            elif stitch_id:
                kwargs["stitch_project_id"] = stitch_id
            result = registry.execute_sync("fetch_design", **kwargs)
            payload = json.loads(result.output) if result.output else {}
            if not payload.get("error"):
                design_context = payload
        except Exception as exc:
            print(f"[team-lead] Design fetch failed: {exc}")

    return {
        "jira_context": jira_context,
        "design_context": design_context,
    }


async def create_plan(state: dict) -> dict:
    """Create a development plan based on analysis and context (LLM single-shot)."""
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
    result = runtime.run(
        prompt=prompt,
        system_prompt=PLANNING_SYSTEM,
        max_tokens=2048,
    )

    raw = result.get("raw_response", "")
    try:
        plan = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        plan = {"steps": [{"step": 1, "action": raw or "Execute task"}]}

    # Build skill context
    skills_registry = state.get("_skills_registry")
    required = state.get("required_skills", [])
    skill_context = ""
    if skills_registry and required:
        skill_context = skills_registry.build_prompt_context(required)

    return {
        "plan": plan,
        "skill_context": skill_context,
    }


async def dispatch_dev_agent(state: dict) -> dict:
    """Dispatch task to a dev agent (Web Dev, Android, etc.) via A2A tool.

    Passes all gathered context so the dev agent does not re-fetch.
    """
    from framework.tools.registry import get_registry

    registry = get_registry()
    revision_feedback = state.get("revision_feedback", "")
    task_description = _build_dev_brief(state)

    try:
        result = registry.execute_sync(
            "dispatch_web_dev",
            task_description=task_description,
            jira_context=state.get("jira_context", {}),
            design_context=state.get("design_context"),
            repo_url=state.get("repo_url", ""),
            revision_feedback=revision_feedback,
        )
        payload = json.loads(result.output) if result.output else {}
    except Exception as exc:
        print(f"[team-lead] Dev dispatch failed: {exc}")
        payload = {"status": "error", "message": str(exc)}

    return {
        "dev_dispatched": True,
        "dev_result": payload,
        "pr_url": payload.get("prUrl", ""),
        "branch_name": payload.get("branch", ""),
    }


async def review_result(state: dict) -> dict:
    """Review the dev agent output via Code Review Agent.

    Returns a route:
      - "approved": review passed
      - "needs_revision": review rejected, revision count < max
      - "need_user_input": max revisions reached, escalate
    """
    from framework.tools.registry import get_registry

    registry = get_registry()
    pr_url = state.get("pr_url", "")
    dev_result = state.get("dev_result", {})

    try:
        result = registry.execute_sync(
            "dispatch_code_review",
            pr_url=pr_url,
            diff_summary=dev_result.get("summary", ""),
            requirements=state.get("analysis_summary", ""),
        )
        payload = json.loads(result.output) if result.output else {}
    except Exception as exc:
        print(f"[team-lead] Code review dispatch failed: {exc}")
        payload = {"verdict": "error", "message": str(exc)}

    verdict = payload.get("verdict", "rejected")
    revision_count = state.get("revision_count", 0)

    if verdict == "approved":
        route = "approved"
    elif revision_count >= state.get("max_revisions", 3):
        route = "need_user_input"
    else:
        route = "needs_revision"

    return {
        "review_result": payload,
        "review_verdict": verdict,
        "route": route,
    }


async def request_revision(state: dict) -> dict:
    """Prepare revision feedback for the dev agent and loop back."""
    review = state.get("review_result", {})
    comments = review.get("comments", [])
    summary = review.get("summary", review.get("message", ""))

    feedback_lines = []
    if summary:
        feedback_lines.append(f"Review summary: {summary}")
    for c in comments[:10]:  # Limit to top 10 comments
        feedback_lines.append(f"- [{c.get('severity', 'info')}] {c.get('message', '')}")

    return {
        "revision_feedback": "\n".join(feedback_lines) or "Code review rejected. Please fix issues.",
        "revision_count": state.get("revision_count", 0) + 1,
    }


async def report_success(state: dict) -> dict:
    """Build final success report."""
    pr_url = state.get("pr_url", "N/A")
    branch = state.get("branch_name", "N/A")
    analysis = state.get("analysis_summary", "")
    verdict = state.get("review_verdict", "approved")
    revision_count = state.get("revision_count", 0)

    return {
        "report_summary": (
            f"Task completed successfully.\n"
            f"Analysis: {analysis}\n"
            f"PR: {pr_url}\n"
            f"Branch: {branch}\n"
            f"Review verdict: {verdict}\n"
            f"Revisions: {revision_count}"
        ),
        "success": True,
    }


async def escalate_to_user(state: dict) -> dict:
    """Escalate to user after max revision attempts."""
    revision_count = state.get("revision_count", 0)
    review = state.get("review_result", {})

    return {
        "report_summary": (
            f"Task requires user intervention after {revision_count} revision attempts.\n"
            f"Last review verdict: {review.get('verdict', 'unknown')}\n"
            f"PR: {state.get('pr_url', 'N/A')}\n"
            f"Please review the remaining issues and provide guidance."
        ),
        "success": False,
        "escalated": True,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_dev_brief(state: dict) -> str:
    """Assemble a comprehensive dev agent brief from all gathered context."""
    parts = [f"Task: {state.get('user_request', '')}"]

    analysis = state.get("analysis_summary", "")
    if analysis:
        parts.append(f"\nAnalysis:\n{analysis}")

    plan = state.get("plan", {})
    if plan:
        parts.append(f"\nPlan:\n{json.dumps(plan, indent=2, ensure_ascii=False)}")

    jira = state.get("jira_context", {})
    if jira:
        parts.append(f"\nJira context:\n{json.dumps(jira, indent=2, ensure_ascii=False)}")

    design = state.get("design_context")
    if design:
        parts.append(f"\nDesign context:\n{json.dumps(design, indent=2, ensure_ascii=False)}")

    skill_ctx = state.get("skill_context", "")
    if skill_ctx:
        parts.append(f"\nSkill guidance:\n{skill_ctx}")

    revision = state.get("revision_feedback", "")
    if revision:
        parts.append(f"\nRevision feedback:\n{revision}")

    return "\n".join(parts)


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
