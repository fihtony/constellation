"""Tests for prompt context budgeting and hygiene."""

from __future__ import annotations

from framework.context_budget import build_jira_brief, compact_delivery_plan, compact_jira_context, text_for_prompt


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


def test_build_jira_brief_keeps_execution_context_and_drops_raw_rest_noise() -> None:
    jira = {
        "expand": "renderedFields,names,schema,operations",
        "key": "ABC-1",
        "names": {"customfield_10042": "Acceptance Criteria"},
        "fields": {
            "summary": "Implement learning dashboard",
            "description": "Build the dashboard. Target repo: https://github.com/acme/study-app",
            "status": {"name": "To Do"},
            "priority": {"name": "High"},
            "issuetype": {"name": "Story"},
            "labels": ["frontend"],
            "components": [{"name": "web"}],
            "customfield_10042": "Dashboard shows weekly progress.",
            "customfield_99999": "x" * 5000,
            "watches": {"watchCount": 42},
        },
    }

    brief = build_jira_brief(
        jira,
        extracted_context={
            "repo_url": "https://github.com/acme/study-app",
            "stitch_project_id": "12345678901234567",
            "stitch_screen_id": "0123456789abcdef0123456789abcdef",
            "tech_stack": ["react", "vite"],
        },
        jira_files=["jira/ABC-1/ticket.json"],
        max_chars=1200,
    )

    assert brief["key"] == "ABC-1"
    assert brief["summary"] == "Implement learning dashboard"
    assert brief["acceptance_criteria"] == "Dashboard shows weekly progress."
    assert brief["repo_url"] == "https://github.com/acme/study-app"
    assert brief["artifact_paths"] == ["jira/ABC-1/ticket.json"]
    serialized = text_for_prompt(brief, max_chars=2000)
    assert "customfield_99999" not in serialized
    assert "watchCount" not in serialized
    assert len(serialized) <= 1200
