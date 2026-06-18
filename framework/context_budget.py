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


def _jira_value_to_text(value: Any, *, max_chars: int) -> str:
    """Convert Jira field values, including Atlassian Document Format, to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return text_for_prompt(value, max_chars=max_chars, default="")
    if isinstance(value, (int, float, bool)):
        return str(value)

    parts: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
            attrs = node.get("attrs")
            if isinstance(attrs, dict):
                for key in ("url", "href"):
                    url = attrs.get(key)
                    if isinstance(url, str) and url.strip():
                        parts.append(url.strip())
            for child in node.get("content") or []:
                _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(value)
    if parts:
        return text_for_prompt("\n".join(parts), max_chars=max_chars, default="")
    return text_for_prompt(value, max_chars=max_chars, default="")


def _named_jira_field_text(fields: dict[str, Any], names: dict[str, Any], *needles: str) -> str:
    """Return text for the first Jira field whose key or display name matches."""
    normalized_needles = tuple(needle.lower() for needle in needles)
    direct_keys = (
        "acceptance_criteria",
        "acceptanceCriteria",
        "acceptance criteria",
        "criteria",
    )
    for key in direct_keys:
        if key in fields:
            text = _jira_value_to_text(fields.get(key), max_chars=4000)
            if text:
                return text

    for key, value in fields.items():
        display = str(names.get(key) or key).lower()
        if all(needle in display for needle in normalized_needles):
            text = _jira_value_to_text(value, max_chars=4000)
            if text:
                return text
    return ""


def _fit_jira_brief_to_budget(brief: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    """Trim optional Jira brief fields until its JSON representation fits."""
    compact = dict(brief)

    def _length() -> int:
        return len(json.dumps(compact, ensure_ascii=False))

    if _length() <= max_chars:
        return compact

    for field in ("description", "acceptance_criteria", "feature_description"):
        value = compact.get(field)
        if not isinstance(value, str) or not value:
            continue
        overflow = _length() - max_chars
        if overflow <= 0:
            return compact
        target = max(120, len(value) - overflow - 64)
        compact[field] = text_for_prompt(value, max_chars=target, default="")
        if _length() <= max_chars:
            return compact

    for field in (
        "recent_comments",
        "labels",
        "components",
        "priority",
        "status",
        "issuetype",
        "stitch_screen_name",
        "figma_url",
    ):
        compact.pop(field, None)
        if _length() <= max_chars:
            return compact

    return compact


def build_jira_brief(
    jira_ctx: Any,
    *,
    extracted_context: dict[str, Any] | None = None,
    jira_files: list[str] | None = None,
    max_chars: int = 3000,
) -> dict[str, Any]:
    """Build a compact cross-agent Jira brief from a raw Jira REST payload.

    Raw Jira tickets remain available as workspace artifacts. This brief is the
    default payload for agent handoffs and LLM prompts so backend-specific context
    behavior does not decide which ticket fields survive.
    """
    if not isinstance(jira_ctx, dict) or not jira_ctx:
        return {}

    fields = jira_ctx.get("fields") if isinstance(jira_ctx.get("fields"), dict) else jira_ctx
    names = jira_ctx.get("names") if isinstance(jira_ctx.get("names"), dict) else {}
    extracted_context = extracted_context or {}

    description = _jira_value_to_text(fields.get("description") or "", max_chars=5000)
    acceptance_criteria = _named_jira_field_text(fields, names, "acceptance")
    if not acceptance_criteria:
        acceptance_criteria = _named_jira_field_text(fields, names, "criteria")

    comments = ((fields.get("comment") or {}) if isinstance(fields.get("comment"), dict) else {}).get("comments")
    recent_comments: list[str] = []
    if isinstance(comments, list):
        for comment in comments[-3:]:
            if not isinstance(comment, dict):
                continue
            body = _jira_value_to_text(comment.get("body", ""), max_chars=280)
            if body:
                recent_comments.append(body)

    brief: dict[str, Any] = {
        "key": str(jira_ctx.get("key") or jira_ctx.get("ticket_key") or "").strip(),
        "summary": str(fields.get("summary") or jira_ctx.get("summary") or "").strip(),
        "description": description,
        "status": (fields.get("status") or {}).get("name", "") if isinstance(fields.get("status"), dict) else str(fields.get("status", "")),
        "priority": (fields.get("priority") or {}).get("name", "") if isinstance(fields.get("priority"), dict) else "",
        "issuetype": (fields.get("issuetype") or {}).get("name", "") if isinstance(fields.get("issuetype"), dict) else "",
        "labels": fields.get("labels", []) if isinstance(fields.get("labels"), list) else [],
        "components": [
            item.get("name", "")
            for item in (fields.get("components") or [])
            if isinstance(item, dict) and item.get("name")
        ],
    }
    if acceptance_criteria:
        brief["acceptance_criteria"] = acceptance_criteria
    if recent_comments:
        brief["recent_comments"] = recent_comments

    for key in (
        "repo_url",
        "figma_url",
        "stitch_project_id",
        "stitch_screen_id",
        "stitch_screen_name",
        "feature_description",
    ):
        value = extracted_context.get(key)
        if value:
            brief[key] = value
    if extracted_context.get("tech_stack"):
        brief["tech_stack"] = extracted_context.get("tech_stack")
    if jira_files:
        brief["artifact_paths"] = [str(path) for path in jira_files if str(path).strip()]

    return _fit_jira_brief_to_budget(
        {key: value for key, value in brief.items() if value not in ("", [], {})},
        max_chars=max_chars,
    )


def compact_jira_context(jira_ctx: Any, *, max_chars: int = 3000) -> str:
    """Serialize only implementation-relevant Jira fields for prompts."""
    if not isinstance(jira_ctx, dict) or not jira_ctx:
        return "N/A"
    return text_for_prompt(build_jira_brief(jira_ctx, max_chars=max_chars), max_chars=max_chars)


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
