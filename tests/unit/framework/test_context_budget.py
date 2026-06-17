"""Tests for prompt context budgeting and hygiene."""

from __future__ import annotations

from framework.context_budget import compact_delivery_plan, compact_jira_context, text_for_prompt


def test_text_for_prompt_strips_reasoning_and_truncates() -> None:
    text = "<think>private reasoning</think>\nVisible task details " + ("x" * 200)

    result = text_for_prompt(text, max_chars=120)

    assert "private reasoning" not in result
    assert result.startswith("Visible task details")
    assert "truncated" in result
    assert len(result) <= 120


def test_compact_delivery_plan_repairs_nested_fenced_plan_action() -> None:
    raw_action = """<think>plan privately</think>
```json
{
  "agent_type": "web-dev",
  "steps": [
    {"step": 1, "action": "Read the design reference"},
    {"step": 2, "action": "Implement the requested page"}
  ]
}
```"""

    plan = compact_delivery_plan({"steps": [{"step": 1, "action": raw_action}]})

    action = plan["steps"][0]["action"]
    assert "plan privately" not in action
    assert "Read the design reference" in action
    assert "Implement the requested page" in action
    assert "```" not in action


def test_compact_jira_context_drops_bulk_rest_payload() -> None:
    jira = {
        "key": "ABC-1",
        "fields": {
            "summary": "Implement feature",
            "description": "short description",
            "comment": {
                "comments": [
                    {"body": "old " + ("x" * 2000)},
                    {"body": "recent actionable note"},
                ]
            },
            "watches": {"watchCount": 99},
        },
    }

    result = compact_jira_context(jira, max_chars=700)

    assert "Implement feature" in result
    assert "recent actionable note" in result
    assert "watchCount" not in result
    assert len(result) <= 700
