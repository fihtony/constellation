"""Compass Agent workflow nodes.

Each node is an async function that receives the workflow state dict and returns
a dict of state updates.  Routing is done by setting ``state["route"]``.
"""
from __future__ import annotations

import json
from typing import Any


# ---------------------------------------------------------------------------
# classify_task — determine task category
# ---------------------------------------------------------------------------

async def classify_task(state: dict) -> dict:
    """Classify user request as development, office, or general."""
    runtime = state.get("_runtime")
    user_request = state.get("user_request", "")

    if not runtime:
        # Fallback heuristic when no LLM runtime
        lower = user_request.lower()
        if any(kw in lower for kw in ["jira", "pr", "branch", "code", "implement", "bug", "feature"]):
            return {"task_classification": "development"}
        if any(kw in lower for kw in ["document", "pdf", "docx", "folder", "file", "summarize"]):
            return {"task_classification": "office"}
        return {"task_classification": "general"}

    from agents.compass.prompts.triage import TRIAGE_SYSTEM, TRIAGE_TEMPLATE

    prompt = TRIAGE_TEMPLATE.format(user_request=user_request)
    result = await runtime.run(
        prompt=prompt,
        system_prompt=TRIAGE_SYSTEM,
        max_tokens=256,
    )

    raw = result.get("raw_response", "general").strip().lower()
    classification = "general"
    if "development" in raw:
        classification = "development"
    elif "office" in raw:
        classification = "office"

    return {"task_classification": classification}


# ---------------------------------------------------------------------------
# check_permissions — validate task is allowed
# ---------------------------------------------------------------------------

async def check_permissions(state: dict) -> dict:
    """Check whether the task is permitted given current permissions."""
    classification = state.get("task_classification", "general")

    # For MVP, all tasks are allowed; full enforcement comes later
    return {
        "permissions_check": {
            "allowed": True,
            "reason": f"Task type '{classification}' is permitted.",
        },
    }


# ---------------------------------------------------------------------------
# dispatch_task — route to downstream agent
# ---------------------------------------------------------------------------

async def dispatch_task(state: dict) -> dict:
    """Set routing key based on task classification."""
    classification = state.get("task_classification", "general")
    return {"route": classification}


# ---------------------------------------------------------------------------
# wait_for_result — wait for downstream agent completion
# ---------------------------------------------------------------------------

async def wait_for_result(state: dict) -> dict:
    """Wait for Team Lead / dev agent result.

    In a real implementation this would poll the downstream A2A task or wait
    for a callback.  For MVP this is a placeholder that expects the result
    to already be set in state by the dispatch step.
    """
    dev_result = state.get("dev_result")
    if dev_result:
        return {"dev_result": dev_result}
    return {}


# ---------------------------------------------------------------------------
# completeness_gate — check if the result is complete
# ---------------------------------------------------------------------------

async def completeness_gate(state: dict) -> dict:
    """Evaluate whether the downstream result is complete."""
    dev_result = state.get("dev_result", {})
    # Team Lead returns camelCase keys in dev_result
    pr_url = dev_result.get("prUrl") or dev_result.get("pr_url")
    success = dev_result.get("success", False)
    jira_in_review = dev_result.get("jiraInReview") or dev_result.get("jira_in_review", False)

    if pr_url and success:
        return {"completeness_score": 1.0, "route": "complete"}

    retry_count = state.get("_completeness_retries", 0) + 1
    max_retries = 2
    if retry_count >= max_retries:
        return {"completeness_score": 0.0, "route": "complete"}  # give up

    return {
        "completeness_score": 0.0,
        "route": "incomplete",
        "_completeness_retries": retry_count,
    }


# ---------------------------------------------------------------------------
# handle_office_task — dispatch to Office Agent
# ---------------------------------------------------------------------------

async def handle_office_task(state: dict) -> dict:
    """Handle office/document tasks via Office Agent."""
    # MVP placeholder — would dispatch to Office Agent via A2A
    return {
        "office_result": {
            "summary": "Office task placeholder.",
        },
    }


# ---------------------------------------------------------------------------
# summarize_for_user — create final user-facing summary
# ---------------------------------------------------------------------------

async def summarize_for_user(state: dict) -> dict:
    """Generate a user-facing summary of the completed task."""
    classification = state.get("task_classification", "general")
    runtime = state.get("_runtime")

    if classification == "general":
        # For general questions, use LLM to answer directly
        if runtime:
            result = await runtime.run(
                prompt=state.get("user_request", ""),
                system_prompt="You are a helpful assistant. Answer the user's question concisely.",
                max_tokens=2048,
            )
            return {"user_summary": result.get("raw_response", "")}
        return {"user_summary": "I'm unable to process this request right now."}

    if classification == "development":
        dev_result = state.get("dev_result", {})
        pr_url = dev_result.get("pr_url", "N/A")
        summary = dev_result.get("summary", "Development task completed.")
        return {"user_summary": f"{summary}\nPR: {pr_url}"}

    if classification == "office":
        office_result = state.get("office_result", {})
        return {"user_summary": office_result.get("summary", "Office task completed.")}

    return {"user_summary": "Task completed."}
