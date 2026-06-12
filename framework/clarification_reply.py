"""Shared clarification reply contracts and resolvers."""

from __future__ import annotations

from typing import Any, Mapping


def build_select_option_contract(
    options: list[dict[str, Any]],
    *,
    reask_message: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "select_option",
        "options": list(options),
        "reask_message": reask_message,
        "ambiguity_policy": "reask",
    }


def build_approve_or_modify_contract(*, modify_requires_note: bool = False) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "approve_or_modify",
        "actions": [
            {"id": "approve", "label": "Approve plan"},
            {"id": "modify", "label": "Modify plan", "requires_note": modify_requires_note},
        ],
        "free_text_suffix": "optional" if not modify_requires_note else "required_for_modify",
        "reask_message": "Please reply with `approve` or `modify: <change>`.",
        "ambiguity_policy": "reask",
    }


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _contract_reask(contract: Mapping[str, Any]) -> str:
    return str(contract.get("reask_message") or "Please reply using one of the supported options.").strip()


def _resolve_select_option(contract: Mapping[str, Any], user_text: str) -> dict[str, Any]:
    normalized_text = _normalize_text(user_text)
    matches: list[str] = []
    for option in contract.get("options") or []:
        if not isinstance(option, Mapping):
            continue
        option_id = _normalize_text(str(option.get("id") or ""))
        label = _normalize_text(str(option.get("label") or ""))
        aliases = [
            _normalize_text(str(alias))
            for alias in (option.get("aliases") or [])
            if str(alias).strip()
        ]
        candidates = [candidate for candidate in [option_id, label, *aliases] if candidate]
        if normalized_text in candidates:
            matches.append(str(option.get("id") or "").strip())
    if len(matches) == 1:
        return {
            "ok": True,
            "normalized": {"selection": matches[0]},
            "diagnostic": "matched_select_option",
        }
    reason = "ambiguous_reply" if len(matches) > 1 else "unknown_reply"
    return {
        "ok": False,
        "reason": reason,
        "reask_message": _contract_reask(contract),
    }


def _resolve_approve_or_modify(contract: Mapping[str, Any], user_text: str) -> dict[str, Any]:
    raw_text = str(user_text or "").strip()
    normalized_text = _normalize_text(raw_text)
    if not normalized_text:
        return {
            "ok": False,
            "reason": "unknown_reply",
            "reask_message": _contract_reask(contract),
        }

    action = ""
    note = ""
    if normalized_text in {"approve", "approved", "yes", "ok", "okay", "go", "y", "lgtm"}:
        action = "approve"
    elif normalized_text in {"modify", "change", "revise", "update"}:
        action = "modify"
    else:
        for prefix in ("modify:", "change:", "revise:", "update:"):
            if normalized_text.startswith(prefix):
                action = "modify"
                note = raw_text.split(":", 1)[1].strip() if ":" in raw_text else ""
                break

    if not action:
        return {
            "ok": False,
            "reason": "unknown_reply",
            "reask_message": _contract_reask(contract),
        }

    action_defs = {
        str(item.get("id") or "").strip(): item
        for item in (contract.get("actions") or [])
        if isinstance(item, Mapping) and item.get("id")
    }
    action_def = action_defs.get(action, {})
    if action == "modify" and not note and ":" in raw_text:
        note = raw_text.split(":", 1)[1].strip()
    if action == "modify" and not note and bool(action_def.get("requires_note")):
        return {
            "ok": False,
            "reason": "missing_required_note",
            "reask_message": _contract_reask(contract),
        }

    return {
        "ok": True,
        "normalized": {"action": action, "note": note},
        "diagnostic": "matched_action_prefix" if note else "matched_action",
    }


def resolve_reply(contract: Mapping[str, Any], user_text: str) -> dict[str, Any]:
    kind = str(contract.get("kind") or "").strip()
    if kind == "select_option":
        return _resolve_select_option(contract, user_text)
    if kind == "approve_or_modify":
        return _resolve_approve_or_modify(contract, user_text)
    if kind == "free_text":
        return {
            "ok": True,
            "normalized": {"text": str(user_text or "").strip()},
            "diagnostic": "accepted_free_text",
        }
    return {
        "ok": False,
        "reason": "stale_contract",
        "reask_message": _contract_reask(contract),
    }
