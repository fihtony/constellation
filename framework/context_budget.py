"""Prompt context hygiene helpers shared by agent workflows.

Agentic backends vary a lot in how they behave with oversized prompts: some
truncate, some drift out of protocol, and some appear to hang until the outer
timeout.  These helpers keep cross-agent handoffs compact and deterministic
without encoding task-specific knowledge.
"""
from __future__ import annotations

import json
from typing import Any

from framework.json_extract import extract_first_json, strip_code_fences, strip_think_blocks

_DEFAULT_SUFFIX = "\n...(truncated; full source remains available in workspace artifacts)"


def text_for_prompt(value: Any, *, max_chars: int, default: str = "N/A") -> str:
    """Return *value* as clean, bounded prompt text.

    The function strips model reasoning blocks and surrounding markdown fences
    before truncating.  Non-string values are serialized as JSON when possible
    so callers can pass already-structured context directly.
    """
    if value is None:
        return default
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)

    text = strip_code_fences(strip_think_blocks(text).strip()).strip()
    if not text:
        return default
    if len(text) <= max_chars:
        return text
    suffix = _DEFAULT_SUFFIX
    head = max(0, max_chars - len(suffix))
    return text[:head].rstrip() + suffix


def compact_jira_context(jira_ctx: Any, *, max_chars: int = 3000) -> str:
    """Serialize only implementation-relevant Jira fields for prompts."""
    if not isinstance(jira_ctx, dict) or not jira_ctx:
        return "N/A"

    fields = jira_ctx.get("fields") if isinstance(jira_ctx.get("fields"), dict) else jira_ctx
    description = fields.get("description") or ""
    if isinstance(description, dict):
        description = text_for_prompt(description, max_chars=5000, default="")

    comments = ((fields.get("comment") or {}) if isinstance(fields.get("comment"), dict) else {}).get("comments")
    recent_comments: list[str] = []
    if isinstance(comments, list):
        for comment in comments[-3:]:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body", "")
            recent_comments.append(text_for_prompt(body, max_chars=280, default=""))

    essential = {
        "key": jira_ctx.get("key", ""),
        "summary": fields.get("summary", ""),
        "description": text_for_prompt(description, max_chars=5000, default=""),
        "status": (fields.get("status") or {}).get("name", "") if isinstance(fields.get("status"), dict) else "",
        "priority": (fields.get("priority") or {}).get("name", "") if isinstance(fields.get("priority"), dict) else "",
        "issuetype": (fields.get("issuetype") or {}).get("name", "") if isinstance(fields.get("issuetype"), dict) else "",
        "labels": fields.get("labels", []),
        "components": [
            item.get("name", "")
            for item in (fields.get("components") or [])
            if isinstance(item, dict)
        ],
    }
    if recent_comments:
        essential["recent_comments"] = recent_comments
    return text_for_prompt(essential, max_chars=max_chars)


def compact_plan_action(action: Any, *, max_chars: int = 900) -> str:
    """Return a compact action string, repairing nested/fenced plan payloads."""
    cleaned = text_for_prompt(action, max_chars=max(max_chars * 4, max_chars), default="")
    parsed = extract_first_json(cleaned)
    if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
        parts: list[str] = []
        for idx, step in enumerate(parsed.get("steps", [])[:12], start=1):
            if isinstance(step, dict):
                raw = step.get("action") or step.get("description") or step
            else:
                raw = step
            part = text_for_prompt(raw, max_chars=240, default="")
            if part:
                parts.append(f"{idx}. {part}")
        if parts:
            cleaned = "\n".join(parts)
    return text_for_prompt(cleaned, max_chars=max_chars, default="Execute task")


def compact_delivery_plan(plan: Any, *, max_steps: int = 12, max_action_chars: int = 900) -> dict[str, Any]:
    """Normalize a delivery plan before it is persisted or handed to a child agent."""
    if not isinstance(plan, dict):
        return {
            "steps": [
                {"step": 1, "action": compact_plan_action(plan, max_chars=max_action_chars)}
            ]
        }

    compact: dict[str, Any] = {}
    for key in ("agent_type", "definition_of_done"):
        if key in plan:
            compact[key] = plan[key]

    steps: list[dict[str, Any]] = []
    raw_steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    for idx, step in enumerate(raw_steps[:max_steps], start=1):
        if isinstance(step, dict):
            action = step.get("action") or step.get("description") or step
            entry = {
                "step": step.get("step", idx),
                "action": compact_plan_action(action, max_chars=max_action_chars),
            }
            if step.get("agent"):
                entry["agent"] = step.get("agent")
        else:
            entry = {
                "step": idx,
                "action": compact_plan_action(step, max_chars=max_action_chars),
            }
        steps.append(entry)

    if not steps:
        steps.append({"step": 1, "action": "Execute task"})
    compact["steps"] = steps
    return compact
