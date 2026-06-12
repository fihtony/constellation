"""Tests for framework.clarification_reply."""

from framework.clarification_reply import resolve_reply


def test_select_option_resolves_alias_to_option_id():
    contract = {
        "kind": "select_option",
        "options": [
            {"id": "workspace", "label": "Workspace", "aliases": ["ws"]},
            {"id": "inplace", "label": "In place", "aliases": ["in place"]},
        ],
    }

    result = resolve_reply(contract, "ws")

    assert result["ok"] is True
    assert result["normalized"]["selection"] == "workspace"


def test_approve_or_modify_returns_modify_with_note():
    contract = {"kind": "approve_or_modify"}

    result = resolve_reply(contract, "modify: use top-level folders first")

    assert result["ok"] is True
    assert result["normalized"]["action"] == "modify"
    assert result["normalized"]["note"] == "use top-level folders first"


def test_unknown_action_reasks():
    contract = {
        "kind": "approve_or_modify",
        "reask_message": "Please reply with `approve` or `modify: <change>`.",
    }

    result = resolve_reply(contract, "sounds good")

    assert result["ok"] is False
    assert result["reason"] == "unknown_reply"
    assert "approve" in result["reask_message"]


def test_missing_required_note_reasks():
    contract = {
        "kind": "approve_or_modify",
        "actions": [
            {"id": "approve", "label": "Approve"},
            {"id": "modify", "label": "Modify", "requires_note": True},
        ],
        "reask_message": "Please reply with `approve` or `modify: <change>`.",
    }

    result = resolve_reply(contract, "modify")

    assert result["ok"] is False
    assert result["reason"] == "missing_required_note"
