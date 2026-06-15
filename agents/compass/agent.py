"""Compass Agent -- LLM-driven control plane entry point.

Architecture: **ReAct-first** (appropriate for open-ended user interaction).

Routing strategy (hybrid — reliable + intelligent):
1. Heuristic classification for obvious development/office tasks (no LLM needed).
2. LLM single-shot classification for ambiguous requests.
3. Direct ToolRegistry dispatch for development/office tasks (deterministic,
   bypasses Claude MCP tool-calling which is unreliable in --print mode).
4. run_agentic (LLM + tools) only for general conversational responses.

Instructions (system prompt) live in:
  agents/compass/instructions/system.md

Tools live in:
  agents/compass/tools.py
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
# Importing framework.devlog early is what activates the default
# timezone fallback (``config/constellation.yaml:default_tz``), so
# every subsequent log line and datetime emission is anchored to the
# right zone even before the agent calls its first AgentLogger.
from framework import devlog  # noqa: F401
from framework.major_step import (
    LIFECYCLE_CANCELLED,
    LIFECYCLE_DONE,
    LIFECYCLE_FAILED,
    LIFECYCLE_RESUMING,
    LIFECYCLE_RUNNING,
    LIFECYCLE_TERMINATED,
    LIFECYCLE_WAITING_FOR_USER,
    record_major_step,
)
from framework.clarification_reply import (
    build_approve_or_modify_contract,
    build_select_option_contract,
    resolve_reply,
)
from agents.compass.ui.routes import handle_ui_request
from agents.compass.tools import TOOL_NAMES, register_compass_tools
from framework.office.dimensions import VALID_DIMENSIONS, parse_dimension


def _build_compass_definition() -> AgentDefinition:
    """Build Compass's AgentDefinition from YAML config, with tool fallback."""
    from framework.config import build_agent_definition_from_config

    try:
        cfg = build_agent_definition_from_config("compass")
    except Exception:
        cfg = {}

    return AgentDefinition(
        agent_id=cfg.get("agent_id", "compass"),
        name=cfg.get("name", "Compass Agent"),
        description=cfg.get(
            "description",
            "Control plane: task classification, permission check, routing, and user summary",
        ),
        mode=AgentMode.CHAT,
        execution_mode=ExecutionMode.PERSISTENT,
        workflow=None,
        tools=cfg.get("tools", TOOL_NAMES),
        permissions=cfg.get("permissions", {"scm": "none", "filesystem": "workspace-only"}),
        permission_profile=cfg.get("permission_profile", "compass"),
        runtime_capabilities=cfg.get("runtime_capabilities", {}),
        config=cfg.get("config", {}),
    )


compass_definition = _build_compass_definition()


def _parse_classification_payload(raw_output: str) -> tuple[str, float]:
    """Parse and validate an LLM triage response.

    Preferred response shape is JSON: {"type": "development", "confidence": 0.9}.
    Legacy one-word responses are accepted for compatibility, but still pass
    through the deterministic classification gate.
    """
    from framework.validation_gates import validate_classification

    raw = (raw_output or "").strip()
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        candidate = str(parsed.get("type") or parsed.get("category") or "").strip().lower()
        try:
            confidence = float(parsed.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 0.0
    else:
        cleaned = raw.strip().lower().strip(".`'\" ")
        candidate = cleaned.split()[0] if cleaned else ""
        confidence = 1.0

    gate = validate_classification(candidate)
    if not gate.passed:
        return "", 0.0
    if confidence < 0 or confidence > 1:
        confidence = 0.0
    return candidate, confidence


def _classify_request(user_text: str, runtime) -> str:
    """Classify request as 'development', 'office', or 'general'.

    Strategy:
    1. Strong heuristics catch unambiguous cases quickly (no LLM call).
    2. Heuristic signals are passed as context hints to the LLM for
       everything else, making the LLM the primary decision maker.
    3. Falls back to 'general' when runtime is unavailable.
    """
    lower = user_text.lower()

    # --- Heuristic pre-screening (high-confidence shortcuts only) ---
    has_jira_url = bool(re.search(
        r"https?://[^\s]+/browse/[A-Z][A-Z0-9]+-\d+", user_text
    ))
    has_jira_key = bool(re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", user_text))
    has_dev_action = any(kw in lower for kw in [
        "implement", "fix bug", "fix the bug", "create pr", "create a pr",
        "open pr", "pull request", "code review", "refactor", "develop",
        "write tests", "add tests", "write unit tests", "set up ci",
        "set up docker", "migrate database", "database migration",
    ])

    # Obvious development: Jira URL + development verb
    if has_jira_url and has_dev_action:
        return "development"
    # Obvious development: explicit implementation request for a Jira ticket
    if has_jira_url and any(kw in lower for kw in ["implement", "implement the", "implement jira"]):
        return "development"
    # Jira key alone is strong enough → development
    if has_jira_key and has_dev_action:
        return "development"

    # Obvious office: document/data operation verbs + file/folder hints
    office_verb = any(kw in lower for kw in ["summarize", "analyze", "organize"])
    office_target = any(kw in lower for kw in [
        "pdf", "docx", "txt", "csv", "xlsx", "xls", "spreadsheet",
        "document", "documents", "folder", "files", "essay", "essays",
    ])
    if office_verb and office_target:
        return "office"
    if any(kw in lower for kw in ["summarize the pdf", "analyze the spreadsheet", "organize files"]):
        return "office"

    # --- LLM-primary classification for everything else ---
    if runtime is None:
        # Unit-test path without runtime: apply minimal fallback heuristics
        if has_jira_url or (has_jira_key and has_dev_action):
            return "development"
        if any(kw in lower for kw in ["summarize", "pdf", "docx", "spreadsheet", "document", "organize files"]):
            return "office"
        return "general"

    try:
        from agents.compass.prompts.triage import TRIAGE_SYSTEM, TRIAGE_TEMPLATE
        result = runtime.run(
            prompt=TRIAGE_TEMPLATE.format(user_request=user_text),
            system_prompt=TRIAGE_SYSTEM,
            max_tokens=128,
        )
        raw = (result.get("raw_response") or "").strip()
        classification, confidence = _parse_classification_payload(raw)
        if classification and confidence >= 0.45:
            return classification
        if classification:
            print(
                f"[compass] LLM triage low confidence: "
                f"classification={classification!r} confidence={confidence:.2f} — defaulting to general"
            )
            return "general"
        # Unexpected output: log and fall back
        print(f"[compass] LLM triage unexpected response: {raw!r} — defaulting to general")
    except Exception as exc:
        print(f"[compass] LLM classification failed: {exc} — defaulting to general")

    return "general"


def _extract_jira_key(user_text: str) -> str:
    """Extract the first Jira issue key from the request text."""
    m = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", user_text)
    return m.group(1) if m else ""


def _normalize_output_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    return mode if mode in {"workspace", "inplace"} else ""


# Neutral, multilingual output-mode phrases that Compass can resolve
# straight from the user request without going through a clarification
# round-trip.  Every entry is a generic phrase; no business-specific or
# test-case wording.  Order matters: longer phrases must come first so
# "in workspace" wins over the bare "workspace" token.
_OUTPUT_MODE_PHRASES: tuple[tuple[str, str], ...] = (
    # workspace
    ("in workspace", "workspace"),
    ("to workspace", "workspace"),
    ("use workspace", "workspace"),
    ("workspace mode", "workspace"),
    ("workspace output", "workspace"),
    ("工作区", "workspace"),
    ("写到工作区", "workspace"),
    ("输出到工作区", "workspace"),
    # inplace
    ("in place", "inplace"),
    ("in-place", "inplace"),
    ("inplace mode", "inplace"),
    ("in the source", "inplace"),
    ("in source folder", "inplace"),
    ("inside the source", "inplace"),
    ("write inside the source", "inplace"),
    ("原地", "inplace"),
    ("原位", "inplace"),
    ("就地", "inplace"),
    ("输出到原", "inplace"),
    ("写到原文件夹", "inplace"),
)


def _scan_output_mode_from_text(user_text: str) -> str:
    """Return ``"workspace"`` / ``"inplace"`` / ``""`` from a user request.

    Scans for neutral multilingual phrases. The agent never invents an
    output mode — when neither phrase matches, the empty string lets
    the caller fall through to the clarification round-trip.  This is
    the symmetric counterpart of
    :func:`framework.office.dimensions.parse_dimension`: both
    validators look at metadata first, then keyword-scan the user
    text, and only fail-closed (return ``""``) when both are silent.
    """
    text = (user_text or "").strip().lower()
    if not text:
        return ""
    # Longest phrase first so "in workspace" wins over a bare "workspace"
    # token that might appear in a different context (e.g. "the workspace
    # output" already covered by the explicit phrase above).
    sorted_phrases = sorted(_OUTPUT_MODE_PHRASES, key=lambda item: -len(item[0]))
    for needle, mode in sorted_phrases:
        if needle in text:
            return mode
    return ""


def _strip_output_mode_phrase(reply: str) -> tuple[str, str, str]:
    """Scan ``reply`` for an output-mode phrase and return
    ``(mode, stripped_reply, matched_phrase)``.

    Used by the cross-aware resume handler so the dimension round can
    pick up an "in place" / "workspace" intent from the user's reply
    without losing it.  When no phrase matches, ``mode`` is empty and
    ``stripped_reply`` is the original reply unchanged.

    Three-step scan so both whole-reply and combined answers work,
    including the bare token that survives dimension stripping:

    1. Whole-reply match against the output-mode contract
       (covers bare ``workspace`` / ``inplace`` / ``in place``).
    2. Substring match against ``_OUTPUT_MODE_PHRASES``
       (covers combined answers like ``by type in place``).
    3. Strip the dimension phrase (using the same machinery as
       :func:`_strip_dimension_phrase`) and re-run the contract on
       what remains.  ``by type workspace`` becomes ``workspace``
       after the dimension scan, which the contract accepts.
    """
    text = (reply or "").strip()
    if not text:
        return "", "", ""

    contract = build_select_option_contract(
        [
            {"id": "workspace", "label": "workspace"},
            {"id": "inplace", "label": "inplace", "aliases": ["in place"]},
        ],
        reask_message="",
    )

    # Step 1: whole-reply match against the contract.
    resolved = resolve_reply(contract, text)
    if resolved.get("ok"):
        mode = str((resolved.get("normalized") or {}).get("selection") or "").strip()
        if mode:
            return mode, "", text

    # Step 2: substring match for combined answers like "by type in place".
    lowered = text.lower()
    sorted_phrases = sorted(_OUTPUT_MODE_PHRASES, key=lambda item: -len(item[0]))
    for needle, mode in sorted_phrases:
        if needle in lowered:
            stripped = (lowered.replace(needle, "", 1)).strip()
            return mode, stripped, needle

    # Step 3: handle the case where a bare ``workspace`` / ``inplace``
    # / ``in place`` token survives the dimension strip.  Example:
    # ``"by type workspace"`` becomes ``"workspace"`` once the
    # ``"by type"`` dimension needle is removed.  The contract accepts
    # the bare token.  Return the dimension-stripped text (i.e. the
    # text with the OUTPUT-MODE token removed) so the caller can
    # resolve the dimension in the same round.
    from framework.office.dimensions import (
        KEYWORD_TO_DIMENSION,
        _CUSTOM_DIMENSION_PATTERNS,
    )
    for needle in sorted(KEYWORD_TO_DIMENSION, key=len, reverse=True):
        if needle in lowered:
            dim_stripped = (lowered.replace(needle, "", 1)).strip()
            # dim_stripped should be the bare mode token, e.g. "workspace".
            leftover_resolved = resolve_reply(contract, dim_stripped)
            if leftover_resolved.get("ok"):
                leftover_mode = str(
                    (leftover_resolved.get("normalized") or {}).get("selection") or ""
                ).strip()
                if leftover_mode:
                    # Re-strip: the caller wants the dimension side
                    # of the reply (e.g. "by type") in stripped_reply
                    # so it can resolve the dimension in this round.
                    dim_only = needle
                    return leftover_mode, dim_only, text
            break
    for pattern in _CUSTOM_DIMENSION_PATTERNS:
        match = pattern.search(text)
        if match:
            dim_stripped = (text[: match.start()] + text[match.end() :]).strip()
            leftover_resolved = resolve_reply(contract, dim_stripped)
            if leftover_resolved.get("ok"):
                leftover_mode = str(
                    (leftover_resolved.get("normalized") or {}).get("selection") or ""
                ).strip()
                if leftover_mode:
                    dim_only = match.group(0)
                    return leftover_mode, dim_only, text
            break
    return "", text, ""


def _strip_dimension_phrase(reply: str) -> tuple[str, str, str]:
    """Scan ``reply`` for a dimension phrase and return
    ``(dimension, stripped_reply, matched_phrase)``.

    Symmetric counterpart of :func:`_strip_output_mode_phrase` — the
    output-mode round uses this to pick up a "by type" / "by size"
    intent from the user's reply without losing it.  Returns
    ``"__custom__"`` (the :data:`CUSTOM_DIMENSION` sentinel) when the
    user wrote a clear "by X" hint that does not match any built-in
    dimension.  When no phrase matches, ``dimension`` is empty and
    ``stripped_reply`` is the original reply unchanged.
    """
    from framework.office.dimensions import (
        CUSTOM_DIMENSION,
        KEYWORD_TO_DIMENSION,
        _CUSTOM_DIMENSION_PATTERNS,
    )

    text = (reply or "").strip()
    if not text:
        return "", "", ""
    lowered = text.lower()
    # Built-in keywords first (longest first so "by file size" wins
    # over the bare "size" token).
    for needle in sorted(KEYWORD_TO_DIMENSION, key=len, reverse=True):
        if needle in lowered:
            dimension = KEYWORD_TO_DIMENSION[needle]
            stripped = (lowered.replace(needle, "", 1)).strip()
            return dimension, stripped, needle
    # Custom-dimension intent (e.g. "by student name") → sentinel.
    for pattern in _CUSTOM_DIMENSION_PATTERNS:
        match = pattern.search(text)
        if match:
            hint = match.group(1).strip()
            if hint:
                stripped = (text[: match.start()] + text[match.end() :]).strip()
                return CUSTOM_DIMENSION, stripped, match.group(0)
    return "", text, ""


def _normalize_organize_dimension(value: str) -> str:
    """Map a free-text user reply to a canonical organize dimension id.

    Accepts:
    - the canonical dimension id (e.g. ``size``, ``modified_time``)
    - a multilingual keyword (e.g. ``大小``, ``按修改时间``) via
      :func:`framework.office.dimensions.parse_dimension`

    Returns the canonical id or ``""`` when the reply is not a recognized
    dimension. The caller is responsible for surfacing the structured
    ``needs_clarification`` question again when the empty result is
    returned.
    """
    text = (value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in VALID_DIMENSIONS:
        return lowered
    return parse_dimension({}, text)


def _normalize_custom_plan_approval(value: str) -> str:
    """Map a user reply to a custom-plan approval action.

    Returns one of:
    - ``"approve"`` — user said yes, run the plan as drafted.
    - ``"modify"``  — user wants changes (the rest of the reply is
      treated as a free-text note the office planner can fold in).
    - ``""``        — unrecognised; the caller re-prompts.
    """
    text = (value or "").strip().lower()
    if not text:
        return ""
    if text in {"approve", "approved", "yes", "ok", "okay", "go", "y", "lgtm"}:
        return "approve"
    if text in {"modify", "change", "revise", "update"} or text.startswith(("modify:", "change:", "revise:")):
        return "modify"
    return ""


def _resolve_office_resume_reply(
    kind: str,
    reply: str,
    office_request: dict,
) -> dict:
    """Validate a user resume reply against the active office interrupt kind.

    Returns a dict with one of:

    - ``{"error_question": str, "needs_clarification": dict}`` when the
      reply did not validate; the caller re-prompts the user with
      ``error_question`` and keeps the task in ``INPUT_REQUIRED``.
    - ``{"office_request": dict}`` when the reply validated; the caller
      applies the update and re-dispatches office.
    """
    office_request = dict(office_request or {})
    if kind == "office_organize_dimension":
        # The custom-dimension plan-approval flow puts the plan in
        # the interrupt metadata's needs_clarification.  When the
        # user replies "approve" / "modify", the reply must NOT be
        # re-validated as a dimension — it is a plan action.
        existing_needs = (office_request.get("_needs_clarification") or {})
        if existing_needs.get("missing") == "organizeCustomPlan":
            reply_contract = dict(existing_needs.get("reply_contract") or {})
            if not reply_contract:
                reply_contract = build_approve_or_modify_contract()
            resolved = resolve_reply(reply_contract, reply)
            if not resolved.get("ok"):
                question = str(
                    resolved.get("reask_message")
                    or "Please reply with `approve` to execute the plan, "
                    "or `modify: <change>` to revise it."
                )
                preserved = dict(existing_needs)
                preserved.update({
                    "missing": "organizeCustomPlan",
                    "options": [
                        {"id": "approve", "label": "Approve plan"},
                        {"id": "modify", "label": "Modify plan"},
                    ],
                    "user_message": question,
                    "reply_contract": reply_contract,
                })
                return {
                    "error_question": question,
                    "needs_clarification": preserved,
                }
            normalized = dict(resolved.get("normalized") or {})
            approval = str(normalized.get("action") or "").strip()
            note = str(normalized.get("note") or "").strip()
            office_request["clarification_resolution"] = {
                "contract_kind": "approve_or_modify",
                "action": approval,
                "note": note,
            }
            office_request["organize_custom_action"] = approval
            plan = dict(existing_needs.get("plan") or {})
            if approval == "approve":
                # Forward the approved plan so the office executor
                # can skip planning and run the LLM classification
                # pass directly.  The plan lives in the interrupt's
                # needs_clarification payload.
                if plan:
                    office_request["organize_custom_plan"] = plan
            if approval == "modify":
                if plan:
                    office_request["organize_custom_plan"] = plan
                office_request["organize_custom_modify_note"] = note or reply
            return {
                "office_request": office_request,
                "resume_payload": {
                    "text": reply,
                    "clarification_resolution": dict(
                        office_request["clarification_resolution"]
                    ),
                },
            }

        # Cross-aware: detect an output mode phrase in the reply and
        # save it.  The user often types the answer to the *next*
        # round while answering the current one (e.g. "in place"
        # typed into the dimension question).  Silently dropping that
        # intent was the bug behind task-51fccd6b57e1.
        output_mode, stripped_reply, _matched_mode_phrase = _strip_output_mode_phrase(reply)
        if output_mode:
            office_request["output_mode"] = output_mode
            # Continue parsing the dimension from the stripped reply
            # so a combined answer (e.g. "by type in place") still
            # resolves the dimension in the same round.
            reply_for_dimension = stripped_reply
        else:
            reply_for_dimension = reply

        dimension = _normalize_organize_dimension(reply_for_dimension)
        if not dimension:
            # If the only intent the user expressed was an output mode
            # phrase (e.g. "in place"), tell them we caught it and
            # surface a re-prompt that still lists the dimensions.
            options = [
                {"id": d, "label": d.replace("_", " ")}
                for d in sorted(VALID_DIMENSIONS)
            ]
            if output_mode:
                user_message = (
                    f"Got it — output mode is `{output_mode}`. "
                    "Now please reply with one of the supported "
                    "organization dimensions: "
                    + ", ".join(sorted(VALID_DIMENSIONS))
                    + "."
                )
            else:
                user_message = (
                    "Please reply with one of the supported organization dimensions: "
                    + ", ".join(sorted(VALID_DIMENSIONS))
                    + "."
                )
            # Return the partial update so the saved output_mode is
            # not lost on the re-prompt round.
            return {
                "error_question": user_message,
                "needs_clarification": {
                    "missing": "organizeGroupBy",
                    "options": options,
                    "user_message": user_message,
                },
                "office_request": office_request,
            }
        office_request["organize_dimension"] = dimension
        # Compass re-dispatches with the dimension in the A2A message
        # metadata; the office workflow reads it from
        # ``metadata.organizeGroupBy`` via ``parse_dimension``.
        office_request.setdefault("organize_metadata", {})
        office_request["organize_metadata"]["organizeGroupBy"] = dimension
        return {"office_request": office_request}

    if kind == "office_output_mode":
        # Cross-aware: detect a dimension phrase in the reply and
        # save it.  Symmetric to the dimension round above — the
        # user often types the answer to the *previous* round while
        # answering the current one (e.g. "by type" typed into the
        # output mode question).
        dimension, stripped_reply, _matched_dim_phrase = _strip_dimension_phrase(reply)
        if dimension:
            office_request["organize_dimension"] = dimension
            office_request.setdefault("organize_metadata", {})
            office_request["organize_metadata"]["organizeGroupBy"] = dimension
            reply_for_mode = stripped_reply
        else:
            reply_for_mode = reply

        contract = build_select_option_contract(
            [
                {"id": "workspace", "label": "workspace"},
                {"id": "inplace", "label": "inplace", "aliases": ["in place"]},
            ],
            reask_message=(
                "Please reply with `workspace` or `inplace` so I can route the "
                "office task correctly."
            ),
        )
        resolved = resolve_reply(contract, reply_for_mode)
        output_mode = str(
            (resolved.get("normalized") or {}).get("selection") or ""
        ).strip()
        if not resolved.get("ok") or not output_mode:
            reask = str(
                resolved.get("reask_message")
                or "Please reply with `workspace` or `inplace` so I can route the "
                "office task correctly."
            )
            if dimension:
                # We caught the dimension but not the output mode —
                # confirm the dimension catch in the re-prompt.
                reask = f"Got it — grouping is `{dimension}`. " + reask
            # Return the partial update so the saved dimension is
            # not lost on the re-prompt round.
            return {"error_question": reask, "office_request": office_request}
        office_request["output_mode"] = output_mode
        return {"office_request": office_request}

    if kind == "office_organizecustomhint":
        from framework.office.dimensions import (
            CUSTOM_DIMENSION,
            extract_custom_dimension_hint,
        )

        hint = str(extract_custom_dimension_hint(reply) or reply or "").strip()
        if not hint:
            question = (
                "Office needs a custom grouping hint, e.g. 'student name' "
                "or 'subject'. Please reply with the entity you want to "
                "group files by."
            )
            return {
                "error_question": question,
                "needs_clarification": {
                    "missing": "organizeCustomHint",
                    "user_message": question,
                },
                "office_request": office_request,
            }
        office_request["organize_dimension"] = CUSTOM_DIMENSION
        office_request.setdefault("organize_metadata", {})
        office_request["organize_metadata"]["organizeGroupBy"] = CUSTOM_DIMENSION
        office_request["organize_metadata"]["customDimensionHint"] = hint
        office_request["customDimensionHint"] = hint
        return {"office_request": office_request}

    if not kind:
        # No active interrupt kind — treat as a no-op; compass will
        # likely re-dispatch with the same arguments. Returning
        # ``error_question`` here would dead-lock the resume.
        return {"office_request": office_request}

    return {
        "error_question": (
            f"Please reply with the requested information for {kind}."
        ),
    }


def _normalize_source_paths(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = [str(item) for item in value if item]
    else:
        return []

    normalized: list[str] = []
    for candidate in candidates:
        sanitized = str(candidate).strip().strip('"\'`').lstrip("([{").rstrip(".,;:!?)]}\"'`")
        if sanitized and sanitized not in normalized:
            normalized.append(sanitized)
    return normalized


def _extract_office_paths_from_text(user_text: str) -> list[str]:
    absolute_paths = re.findall(r'(?:(?<=\s)|^)(/[^\s"\'`]+)', user_text or "")
    quoted_paths = re.findall(r'["\']([^"\']*[\\/][^"\']+)["\']', user_text or "")
    paths = [candidate for candidate in absolute_paths + quoted_paths if not candidate.startswith("//")]
    return _normalize_source_paths(paths)


def _normalize_office_capability(value: str, user_text: str = "") -> str:
    raw = (value or "").strip().lower()
    mapping = {
        "office.document.summarize": "summarize",
        "office.folder.summarize": "summarize",
        "office.data.analyze": "analyze",
        "office.folder.organize": "organize",
    }
    raw = mapping.get(raw, raw)
    if raw in {"summarize", "analyze", "organize"}:
        return raw
    lower = user_text.lower()
    if "organize" in lower:
        return "organize"
    if "analyze" in lower:
        return "analyze"
    return "summarize"


def _office_requested_capability(capability: str) -> str:
    mapping = {
        "analyze": "office.data.analyze",
        "organize": "office.folder.organize",
        "summarize": "office.document.summarize",
    }
    return mapping.get(capability, "office.document.summarize")


def _extract_office_request(user_text: str, metadata: dict) -> dict:
    source_paths = _normalize_source_paths(
        metadata.get("source_paths") or metadata.get("officeTargetPaths") or metadata.get("filePath")
    )
    if not source_paths:
        source_paths = _extract_office_paths_from_text(user_text)

    # Resolve the dimension up front so the office agent receives a
    # uniform ``organize_metadata.organizeGroupBy`` payload — whether
    # the dimension is one of the six built-in ones or the
    # ``__custom__`` sentinel that routes through the LLM planner.
    from framework.office.dimensions import (
        CUSTOM_DIMENSION,
        extract_custom_dimension_hint,
        parse_dimension,
    )

    capability = _normalize_office_capability(
        str(metadata.get("capability") or metadata.get("requestedCapability") or ""),
        user_text,
    )
    organize_group_by = ""
    organize_metadata: dict = {}
    if capability == "organize":
        organize_group_by = parse_dimension(
            {"organizeGroupBy": metadata.get("organizeGroupBy", "")},
            user_text,
        )
        if organize_group_by:
            organize_metadata = {"organizeGroupBy": organize_group_by}
            if organize_group_by == CUSTOM_DIMENSION:
                # Forward the user's natural-language hint so office's
                # planner knows what to bucket by.  Pull it from
                # metadata if the orchestrator pinned it, otherwise
                # re-extract from the user text.
                hint = str(
                    metadata.get("customDimensionHint")
                    or extract_custom_dimension_hint(user_text)
                    or ""
                ).strip()
                if hint:
                    organize_metadata["customDimensionHint"] = hint
    return {
        "source_paths": source_paths,
        "capability": capability,
        "output_mode": (
            _normalize_output_mode(
                str(metadata.get("output_mode") or metadata.get("officeOutputMode") or "")
            )
            or _scan_output_mode_from_text(user_text)
        ),
        "organize_dimension": organize_group_by,
        "organize_metadata": organize_metadata,
    }


def _office_major_step_skeleton(office_request: dict) -> list[dict]:
    """Return the proposal-aligned office timeline skeleton for Compass UI."""
    capability = _normalize_office_capability(str(office_request.get("capability") or ""))

    rows: list[dict] = [
        {
            "step_key": "compass.received",
            "title": "Compass receiving task",
            "agent": "compass",
        },
        {
            "step_key": "compass.asking_output_mode",
            "title": "Compass asking for output location",
            "agent": "compass",
            "conditional": True,
        },
        {
            "step_key": "compass.dispatched",
            "title": "Compass dispatching to Office Agent",
            "agent": "compass",
        },
        {
            "step_key": "office.received",
            "title": "Office receiving task",
            "agent": "office",
        },
        {
            "step_key": "office.validating",
            "title": "Office validating sources and permissions",
            "agent": "office",
        },
    ]

    if capability == "analyze":
        rows.extend(
            [
                {
                    "step_key": "office.inferring_schema",
                    "title": "Office inferring data schema",
                    "agent": "office",
                },
                {
                    "step_key": "office.computing_stats",
                    "title": "Office computing statistics",
                    "agent": "office",
                },
                {
                    "step_key": "office.generating_report",
                    "title": "Office generating analysis report",
                    "agent": "office",
                },
                {
                    "step_key": "office.writing",
                    "title": "Office writing deliverable",
                    "agent": "office",
                },
            ]
        )
    elif capability == "organize":
        rows.extend(
            [
                {
                    "step_key": "office.scanning",
                    "title": "Office scanning folder structure",
                    "agent": "office",
                },
                {
                    "step_key": "office.planning",
                    "title": "Office planning organization",
                    "agent": "office",
                },
                {
                    "step_key": "office.creating_folders",
                    "title": "Office creating folder structure",
                    "agent": "office",
                },
                {
                    "step_key": "office.moving_files",
                    "title": "Office moving files into organized structure",
                    "agent": "office",
                },
                {
                    "step_key": "office.writing_plan",
                    "title": "Office writing organization plan",
                    "agent": "office",
                },
            ]
        )
    else:
        rows.extend(
            [
                {
                    "step_key": "office.reading",
                    "title": "Office reading documents",
                    "agent": "office",
                },
                {
                    "step_key": "office.summarizing",
                    "title": "Office summarizing each document",
                    "agent": "office",
                },
                {
                    "step_key": "office.combining",
                    "title": "Office creating combined summary",
                    "agent": "office",
                    "conditional": True,
                },
                {
                    "step_key": "office.writing",
                    "title": "Office writing deliverable",
                    "agent": "office",
                },
            ]
        )

    rows.extend(
        [
            {
                "step_key": "office.validating_plan_output",
                "title": "Office validating output against plan",
                "agent": "office",
            },
            {
                "step_key": "office.reconciling_plan_output",
                "title": "Office reconciling output to match plan",
                "agent": "office",
                "conditional": True,
            },
            {
                "step_key": "office.gate_exhausted",
                "title": "Office plan-output gate exhausted",
                "agent": "office",
                "conditional": True,
            },
            {
                "step_key": "office.verifying",
                "title": "Office verifying deliverable",
                "agent": "office",
            },
            {
                "step_key": "office.delivered",
                "title": "Office delivering report to Compass",
                "agent": "office",
            },
        ]
    )
    return rows


def _development_major_step_skeleton(jira_key: str = "") -> list[dict]:
    """Return the proposal-aligned development timeline skeleton for Compass UI."""
    return [
        {
            "step_key": "compass.received",
            "title": "Compass receiving task",
            "agent": "compass",
        },
        {
            "step_key": "compass.dispatched",
            "title": "Compass dispatching to Team Lead",
            "agent": "compass",
        },
        {
            "step_key": "tl.analyzing",
            "title": "Team Lead analyzing task",
            "agent": "team-lead",
        },
        {
            "step_key": "tl.gathering",
            "title": "Team Lead gathering context",
            "agent": "team-lead",
        },
        {
            "step_key": "tl.dispatched_dev",
            "title": "Team Lead dispatching to Web Dev",
            "agent": "team-lead",
        },
        {
            "step_key": "wd.drafting_plan",
            "title": "Web Dev drafting plan",
            "agent": "web-dev",
        },
        {
            "step_key": "wd.implementing",
            "title": "Web Dev implementing changes",
            "agent": "web-dev",
        },
        {
            "step_key": "wd.building",
            "title": "Web Dev building and testing",
            "agent": "web-dev",
        },
        {
            "step_key": "wd.self_check",
            "title": "Web Dev running self-check",
            "agent": "web-dev",
        },
        {
            "step_key": "wd.handover",
            "title": "Web Dev handing over to Team Lead",
            "agent": "web-dev",
        },
        {
            "step_key": "tl.requesting_review",
            "title": "Team Lead requesting code review",
            "agent": "team-lead",
        },
        {
            "step_key": "cr.reviewing",
            "title": "Code Review reviewing PR",
            "agent": "code-review",
        },
        {
            "step_key": "tl.reported",
            "title": "Team Lead reporting to Compass",
            "agent": "team-lead",
        },
        {
            "step_key": "compass.task_completed",
            "title": "Compass marking task completed",
            "agent": "compass",
        },
        {
            "step_key": "tl.requesting_changes",
            "title": "Team Lead requesting changes from Web Dev",
            "agent": "team-lead",
            "conditional": True,
        },
        {
            "step_key": "wd.addressing_feedback",
            "title": "Web Dev addressing review feedback",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "wd.fixing_gaps",
            "title": "Web Dev fixing self-check gaps",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "wd.rebuilding",
            "title": "Web Dev rebuilding and retesting",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "wd.self_check_retry",
            "title": "Web Dev rerunning self-check",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "wd.handover_retry",
            "title": "Web Dev handing over revised result",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "tl.re_requesting_review",
            "title": "Team Lead requesting follow-up code review",
            "agent": "team-lead",
            "conditional": True,
        },
        {
            "step_key": "cr.reviewing_retry",
            "title": "Code Review reviewing revised PR",
            "agent": "code-review",
            "conditional": True,
        },
        {
            "step_key": "tl.requesting_user_input",
            "title": "Team Lead requesting user input for clarification",
            "agent": "team-lead",
            "conditional": True,
        },
        {
            "step_key": "wd.requesting_user_input",
            "title": "Web Dev requesting user input",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "cr.requesting_user_input",
            "title": "Code Review requesting user input",
            "agent": "code-review",
            "conditional": True,
        },
    ]


def _office_output_mode_question() -> str:
    return (
        "Where should the office output go? Reply `workspace` to keep the source read-only and "
        "write results under the task workspace, or reply `inplace` to write inside the source folder."
    )


def _gate_output_mode_round(
    *,
    task_id: str,
    office_request: dict,
    task_store,
    log,
    aid: str,
    capability_label: str,
) -> dict | None:
    """Ask the output-mode question when the office_request still has
    an empty ``output_mode``.

    Used by both the init path (line 1817-1864) and the resume path
    (line 2232 onwards) so the same gate fires in both flows.  The
    resume path silently defaulted to ``"workspace"`` and dispatched
    without asking; that bug is the reason this helper exists.

    Returns:
      - ``None`` when ``output_mode`` is set, meaning the caller
        should proceed to dispatch.
      - a ``ui_update`` dict (with ``task_status="TASK_STATE_INPUT_REQUIRED"``)
        when the task was paused; the caller must return it directly
        instead of dispatching.

    The caller is responsible for forwarding the new state to the
    user.  No background workers are spawned from this helper.
    """
    if office_request.get("output_mode"):
        return None
    question = _office_output_mode_question()
    task_store.pause_task(
        task_id,
        question=question,
        interrupt_metadata={
            "kind": "office_output_mode",
            "office_request": dict(office_request),
        },
    )
    _record_major_step(
        task_store,
        task_id,
        step_key="compass.asking_output_mode",
        title="Compass asking for output location",
        agent=aid,
        lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
        conditional=True,
        summary_template="Compass is waiting for you to choose the output location.",
    )
    _append_chat_entry(
        task_store,
        task_id,
        role="COMPASS",
        text=question,
        tone="input-required",
    )
    log.info(
        "office task awaiting output mode",
        capability=capability_label,
        source_count=len(office_request.get("source_paths", []) or []),
    )
    return {
        "task_id": task_id,
        "ui_update": {
            "task_id": task_id,
            "task_status": "TASK_STATE_INPUT_REQUIRED",
            "chat_message": {
                "role": "COMPASS",
                "text": question,
                "style": "normal",
            },
        },
    }


def _office_dimension_resolved(user_text: str, office_request: dict) -> bool:
    """Return True when the organize request already carries a usable
    grouping dimension (either pinned in metadata or scanned from the
    user text).  The check mirrors the office agent's own
    :func:`framework.office.dimensions.parse_dimension` so the two
    sides agree on what counts as "resolved".
    """
    from framework.office.dimensions import parse_dimension

    # Check the organize_metadata.organizeGroupBy (the canonical
    # source of truth) first; fall back to organize_dimension and
    # the top-level legacy key.  The previous version only looked
    # at the top-level key, which was always empty after the
    # dimension-first refactor, so the gate fired even when the
    # office request had already pinned a custom dimension.
    metadata = {
        "organizeGroupBy": (
            (office_request.get("organize_metadata") or {}).get("organizeGroupBy")
            or office_request.get("organize_dimension")
            or office_request.get("organizeGroupBy")
            or ""
        )
    }
    if parse_dimension(metadata, user_text):
        return True
    return False


def _office_organize_dimension_question(user_text: str) -> str:
    """Render a user-facing question for the missing dimension.

    Unlike the bare dimension list, this surfaces the user's last
    natural-language hint so the user can see *why* their phrasing did
    not match.  For example, "by student name" gets acknowledged as
    not-supported and the 6 valid options are listed.
    """
    options_block = ", ".join(
        f"`{dim}`" for dim in sorted(VALID_DIMENSIONS)
    )
    text = (user_text or "").lower()
    has_no_dimension_signal = not parse_dimension({}, user_text)
    if has_no_dimension_signal:
        # The user said something that does not match any of the six
        # supported dimensions.  Name the unsupported hint and tell
        # them explicitly which dimensions are available.
        return (
            "I could not find a supported grouping dimension in the "
            "request. The organize capability groups files by one of "
            f"{options_block}, not by arbitrary entities like student "
            "name. Please reply with one of the supported dimensions."
        )
    return (
        "Office organize needs a grouping dimension. "
        f"Available dimensions: {options_block}."
    )


def _office_callback_url(task_id: str) -> str:
    base_url = os.environ.get("COMPASS_BASE_URL", "").rstrip("/")
    if not base_url:
        return ""
    return f"{base_url}/tasks/{task_id}/callbacks"


def _office_delivery_report_path(task_id: str) -> str:
    artifact_root = os.environ.get("ARTIFACT_ROOT", "artifacts/")
    return os.path.join(artifact_root, task_id, "office", "task-report.json")


def _office_dispatch_failed(dispatch_data: dict[str, Any]) -> bool:
    if str(dispatch_data.get("status") or "").strip().lower() in {
        "error",
        "failed",
        "no-capability",
        "unknown",
    }:
        return True
    # Belt-and-suspenders: if the LLM wrote an error explanation instead of
    # real output, the office agent may still report status="completed" because
    # the agentic runtime only checks that *some* response was produced.  Treat
    # such summaries as failures so the orchestrator surfaces them honestly.
    summary = str(
        dispatch_data.get("summary")
        or dispatch_data.get("message")
        or ""
    ).strip()
    return _summary_indicates_office_failure(summary)


def _office_dispatch_awaiting_input(dispatch_data: dict[str, Any]) -> bool:
    """Return True when office returned ``input-required``.

    Detects either the normalized status string from
    :func:`_dispatch_office_task_via_launcher` (``"input-required"``) or
    the raw A2A state (``"TASK_STATE_INPUT_REQUIRED"``). A clarification
    payload is also required to avoid treating a transient ""status" as
    an input request.
    """
    status = str(dispatch_data.get("status") or "").strip().lower()
    state = str(dispatch_data.get("state") or "").strip()
    if status != "input-required" and state != "TASK_STATE_INPUT_REQUIRED":
        return False
    needs_clarification = dispatch_data.get("needs_clarification")
    if not needs_clarification:
        return False
    return isinstance(needs_clarification, dict) and bool(
        needs_clarification.get("missing")
    )


def _office_interrupt_kind(
    office_request: dict[str, Any],
    clarification_payload: dict[str, Any],
) -> str:
    """Translate a needs_clarification payload into an ``interrupt_metadata.kind`` slug.

    The slug drives both the major-step step_key (e.g.
    ``compass.office_organize_dimension``) and the ``resume_task``
    routing. New clarification types should add their own branch here.
    """
    missing = str(clarification_payload.get("missing") or "").strip()
    capability = str(office_request.get("capability") or "").strip().lower()
    if missing == "organizeGroupBy" or (
        capability == "organize" and missing in {"", "organizeGroupBy"}
    ):
        return "office_organize_dimension"
    if missing == "organizeCustomPlan":
        # Plan approval lives under the same dimension gate so the
        # major-step timeline shows one continuous "asking for
        # dimension" row that transitions from "pick a dimension"
        # to "approve the plan" without a UI flicker.
        return "office_organize_dimension"
    if missing:
        return f"office_{missing.lower()}"
    return "office_clarification"


def _office_clarification_default_question(clarification_payload: dict[str, Any]) -> str:
    """Render a user-facing question for a ``needs_clarification`` payload.

    Used when the office task did not supply a ``user_message`` (rare —
    the office gate always sets one). Falls back to a generic prompt that
    lists the supported options so the user can still reply meaningfully.
    """
    options = clarification_payload.get("options") or []
    if not options:
        return "Office needs more information before it can continue."
    option_lines = "\n".join(
        f"- `{opt.get('id')}` — {opt.get('label') or opt.get('id')}"
        for opt in options
        if isinstance(opt, dict) and opt.get("id")
    )
    return (
        "Office needs more information before it can continue.\n"
        "Please reply with one of:\n"
        f"{option_lines}"
    )


_OFFICE_FAILURE_PATTERNS = (
    "cannot be found or accessed",
    "could not be found",
    "does not exist or is not a valid",
    "error encountered",
    "i cannot inspect or analyze",
    "i cannot access",
    "no such file or directory",
    "required action",
    "source file is not accessible",
    "the path does not exist",
    "the file does not exist",
    "file not found",
    "the requested source file cannot",
)


def _summary_indicates_office_failure(summary: str) -> bool:
    if not summary:
        return False
    lowered = summary.lower()
    return any(needle in lowered for needle in _OFFICE_FAILURE_PATTERNS)


def _dispatch_office_request(task_id: str, user_text: str, office_request: dict, registry, log) -> dict:
    registry_url = ""
    office_url = ""
    discovered_from_registry = False
    requested_capability = _office_requested_capability(office_request.get("capability", "summarize"))
    try:
        from framework.registry_client import RegistryClient

        rc = RegistryClient.from_config()
        registry_url = rc.url
        log.a2a("→", "registry", capability=requested_capability, registry_url=registry_url)
        # Office is now an on-demand agent: compass always launches a
        # per-task container via the Launcher, never talks to a
        # long-running office service. The only thing we need from the
        # registry is the launch definition (image, port, env); if it's
        # not registered, fail closed.
        definition = rc.get_capability_definition(requested_capability)
        if not definition:
            definition = rc.get_capability_definition("office.document.summarize")
        discovered_from_registry = bool(definition)
        office_url = "per-task-launch" if discovered_from_registry else ""
        log.info("registry lookup", registry_url=registry_url, discovered_url=office_url)
        log.a2a(
            "←",
            "registry",
            capability=requested_capability,
            status="found" if discovered_from_registry else "not_found",
            discovered_url=office_url,
        )
    except Exception as exc:
        log.warn("registry lookup failed", error=str(exc))
        log.a2a("←", "registry", capability=requested_capability, status="error", error=str(exc)[:100])

    if not discovered_from_registry:
        log.warn("office capability not found in registry", registry_url=registry_url)
        return {
            "status": "no-capability",
            "message": "Constellation currently has no registered office-processing agent, so this office task cannot run right now.",
        }

    callback_url = _office_callback_url(task_id)
    log.a2a(
        "→",
        "office",
        capability=requested_capability,
        office_url=office_url,
        task_id=task_id,
        source_count=len(office_request.get("source_paths", [])),
        output_mode=office_request.get("output_mode", "workspace"),
    )
    # Pull the user-resolved dimension (set after a clarification round-trip)
    # out of the office_request. ``organize_metadata.organizeGroupBy`` is
    # the source of truth so the A2A metadata reflects the latest user reply.
    organize_group_by = ""
    if str(office_request.get("capability", "")).strip().lower() == "organize":
        organize_group_by = str(
            (office_request.get("organize_metadata") or {}).get("organizeGroupBy")
            or office_request.get("organize_dimension")
            or ""
        ).strip()
    try:
        dispatch_args: dict[str, Any] = {
            "task_description": user_text,
            "source_paths": office_request.get("source_paths", []),
            "capability": office_request.get("capability", "summarize"),
            "output_mode": office_request.get("output_mode", "workspace"),
            "orchestrator_task_id": task_id,
            "callback_url": callback_url,
        }
        if organize_group_by:
            dispatch_args["organizeGroupBy"] = organize_group_by
        # Custom-dimension plan-then-execute state.  The plan
        # approval action + the approved plan live on
        # ``office_request`` after the user has approved via compass
        # resume.  Forward them to office so the executor runs
        # directly without re-planning.
        custom_hint = str(
            (office_request.get("organize_metadata") or {}).get("customDimensionHint")
            or office_request.get("customDimensionHint")
            or ""
        ).strip()
        if custom_hint:
            dispatch_args["customDimensionHint"] = custom_hint
        custom_plan = office_request.get("organize_custom_plan") or {}
        if custom_plan:
            dispatch_args["organizeCustomPlan"] = dict(custom_plan)
        custom_action = str(office_request.get("organize_custom_action") or "").strip()
        if custom_action:
            dispatch_args["organizeCustomAction"] = custom_action
        custom_modify_note = str(
            office_request.get("organize_custom_modify_note") or ""
        ).strip()
        if custom_modify_note:
            dispatch_args["organizeCustomModifyNote"] = custom_modify_note
        dispatch_result_str = registry.execute_sync(
            "dispatch_office_task",
            dispatch_args,
        )
        dispatch_data = json.loads(dispatch_result_str) if dispatch_result_str else {}
        log.a2a("←", "office", status=dispatch_data.get("status", "unknown"), result_preview=str(dispatch_data)[:200])
    except Exception as exc:
        dispatch_data = {"status": "error", "message": str(exc)}
        log.error("dispatch_office_task failed", error=str(exc))
        log.a2a("←", "office", status="error", error=str(exc)[:100])
        print(f"[compass] dispatch_office_task error: {exc}")

    report_path = _office_delivery_report_path(task_id)
    if dispatch_data.get("status") == "completed":
        if os.path.exists(report_path):
            dispatch_data["deliveryVerified"] = True
            dispatch_data["deliveryReportPath"] = report_path
            report_success = True
            try:
                with open(report_path, encoding="utf-8") as fh:
                    report_data = json.load(fh)
                report_success = bool((report_data.get("data") or {}).get("success", True))
            except Exception as exc:
                report_success = False
                dispatch_data["message"] = dispatch_data.get("message") or (
                    "Office task reported completion but task-report.json could not be read."
                )
                log.warn("office delivery report unreadable", task_report=report_path, error=str(exc))

            if report_success:
                log.info("office delivery verified", task_report=report_path)
            else:
                dispatch_data["status"] = "failed"
                if not dispatch_data.get("summary"):
                    summary = str((report_data.get("data") or {}).get("summary") or "").strip() if 'report_data' in locals() else ""
                    if summary:
                        dispatch_data["summary"] = summary
                log.warn("office delivery report indicated failure", task_report=report_path)
        else:
            log.warn("office delivery report missing", task_report=report_path)
            summary = str(dispatch_data.get("summary") or dispatch_data.get("message") or "").strip()
            dispatch_data["status"] = "failed"
            failure_reason = "Office task reported completion but did not write task-report.json."
            dispatch_data["message"] = (
                f"{summary}\n\n{failure_reason}" if summary else failure_reason
            )

    # Final guard: if the LLM produced an error explanation instead of real
    # output, downgrade status even if the office agent claimed success.
    if (
        str(dispatch_data.get("status") or "").strip().lower() == "completed"
        and _summary_indicates_office_failure(
            str(dispatch_data.get("summary") or dispatch_data.get("message") or "")
        )
    ):
        dispatch_data["status"] = "failed"
        log.warn(
            "office summary indicated failure despite completed status",
            summary_preview=str(dispatch_data.get("summary") or "")[:200],
        )

    log.info("office dispatch complete", status=dispatch_data.get("status", "unknown"))
    return dispatch_data


def _office_session_from_metadata(metadata: dict[str, Any] | None, task_id: str) -> dict[str, Any]:
    metadata = metadata or {}
    session = metadata.get("office_session") or {}
    if isinstance(session, dict) and session.get("service_url"):
        merged = dict(session)
        merged.setdefault("task_id", task_id)
        merged.setdefault("agent_id", "office")
        return merged
    office_service_url = str(metadata.get("office_service_url") or "").strip()
    if not office_service_url:
        return {}
    return {
        "task_id": task_id,
        "service_url": office_service_url,
        "container_name": str(metadata.get("office_container_name") or "").strip(),
        "agent_id": "office",
    }


def _office_task_to_dispatch_data(
    office_response: dict[str, Any],
    office_session: dict[str, Any],
) -> dict[str, Any]:
    task = office_response.get("task", office_response)
    if not isinstance(task, dict):
        task = {}
    task_state = str((task.get("status") or {}).get("state") or "").strip()
    artifacts = task.get("artifacts") or []
    summary = _extract_text(artifacts) or _extract_status_text(task) or "Task completed."
    status = "completed" if task_state == "TASK_STATE_COMPLETED" else (
        "input-required" if task_state == "TASK_STATE_INPUT_REQUIRED" else "error"
    )
    interrupt_metadata = task.get("metadata", {}).get("_interrupt") or {}
    needs_clarification = (
        interrupt_metadata.get("needs_clarification")
        if isinstance(interrupt_metadata, dict) else None
    )
    question = ""
    if isinstance(needs_clarification, dict):
        question = str(needs_clarification.get("user_message") or "").strip()
    if not question:
        question = _extract_status_text(task)
    return {
        "status": status,
        "state": task_state,
        "taskId": task.get("id", ""),
        "summary": summary,
        "message": summary,
        "question": question,
        "needs_clarification": needs_clarification or {},
        "office_service_url": str(office_session.get("service_url") or "").strip(),
        "office_session": dict(office_session or {}),
    }


def _extract_text(artifacts: list[dict]) -> str:
    for art in artifacts:
        for part in art.get("parts", []):
            if "text" in part:
                return part["text"]
    return ""


def _extract_status_text(task: dict) -> str:
    parts = task.get("status", {}).get("message", {}).get("parts", [])
    for part in parts:
        if "text" in part:
            return part["text"]
    return ""


def _development_start_message(jira_key: str) -> str:
    jira_label = jira_key or "N/A"
    return (
        "Development task accepted and running in the background.\n"
        f"Jira: {jira_label}"
    )


def _development_final_message(dispatch_data: dict) -> str:
    summary = str(dispatch_data.get("summary") or "").strip()
    if summary:
        return summary
    status = str(dispatch_data.get("status") or "unknown").strip() or "unknown"
    if status == "completed":
        pr_url = str(dispatch_data.get("prUrl") or "").strip()
        branch = str(dispatch_data.get("branch") or "").strip()
        lines = ["Development task completed successfully."]
        if pr_url:
            lines.append(f"PR: {pr_url}")
        if branch:
            lines.append(f"Branch: {branch}")
        return "\n".join(lines)
    return str(dispatch_data.get("message") or f"Development task ended with status: {status}").strip()


def _chat_entry_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_chat_entry(
    task_store,
    task_id: str,
    *,
    role: str,
    text: str,
    tone: str = "normal",
) -> None:
    if not text:
        return
    task = task_store.get_task(task_id)
    if task is None:
        return

    metadata = task.metadata or {}
    history = list(metadata.get("chat_history") or [])
    history.append(
        {
            "role": role,
            "text": text,
            "tone": tone,
            "ts": _chat_entry_timestamp(),
        }
    )
    task_store.update_metadata(task_id, {"chat_history": history})


def _record_major_step(
    task_store,
    task_id: str,
    *,
    step_key: str,
    title: str,
    agent: str = "compass",
    lifecycle_state: str = LIFECYCLE_RUNNING,
    summary_template: str = "",
    summary_facts: dict | None = None,
    conditional: bool = False,
    round: int = 0,
) -> None:
    """Thin Compass-side wrapper around ``framework.major_step.record_major_step``.

    Compass owns the same ``TaskStore`` as the orchestrator task, so the
    ``orchestrator_task_id`` equals ``task_id`` and the cross-process
    fan-out branch is a no-op.
    """
    if not task_id or not step_key or not title:
        return
    if task_store.get_task(task_id) is None:
        return
    record_major_step(
        task_id,
        step_key=step_key,
        title=title,
        agent=agent,
        lifecycle_state=lifecycle_state,
        summary_template=summary_template,
        summary_facts=summary_facts,
        conditional=conditional,
        round=round,
        orchestrator_task_id=task_id,
        task_store=task_store,
    )


def _log_store_url() -> str:
    return (
        os.environ.get("LOG_STORE_URL")
        or os.environ.get("LOG_STORE_BASE_URL")
        or ""
    ).rstrip("/")


class CompassAgent(BaseAgent):
    """Compass Agent -- routes requests via heuristic + LLM classification."""

    def _complete_development_task(
        self,
        *,
        task_id: str,
        user_text: str,
        jira_key: str,
        workspace_path: str,
    ) -> None:
        from framework.a2a.protocol import Artifact
        from framework.devlog import AgentLogger
        from framework.tools.registry import get_registry

        task_store = self.services.task_store
        log = AgentLogger(task_id=task_id, agent_name=self.definition.agent_id)
        dispatch_data: dict[str, object] = {}

        try:
            registry = get_registry()
            log.a2a("→", "team-lead", capability="dispatch_development_task", jira_key=jira_key)
            dispatch_result_str = registry.execute_sync(
                "dispatch_development_task",
                {
                    "task_description": user_text,
                    "jira_key": jira_key,
                    "orchestratorTaskId": task_id,
                    "workspacePath": workspace_path,
                },
            )
            dispatch_data = json.loads(dispatch_result_str) if dispatch_result_str else {}
        except Exception as exc:
            dispatch_data = {"status": "error", "message": str(exc)}
            log.error("dispatch_development_task failed", error=str(exc))
            print(f"[{self.definition.agent_id}] dispatch_development_task error: {exc}")

        team_lead_task_id = str(dispatch_data.get("taskId") or "").strip()
        if team_lead_task_id:
            task_store.update_metadata(task_id, {"teamLeadTaskId": team_lead_task_id})

        final_message = _development_final_message(dispatch_data)
        artifact_metadata = {"agentId": self.definition.agent_id}
        if team_lead_task_id:
            artifact_metadata["teamLeadTaskId"] = team_lead_task_id
        for key in ("prUrl", "branch", "jiraInReview"):
            value = dispatch_data.get(key)
            if value not in (None, ""):
                artifact_metadata[key] = value

        artifacts = [Artifact(
            name="compass-response",
            artifact_type="text/plain",
            parts=[{"text": final_message}],
            metadata=artifact_metadata,
        )]

        task_state = str(dispatch_data.get("state") or "").strip()
        status = str(dispatch_data.get("status") or "unknown").strip() or "unknown"
        if task_state == "TASK_STATE_INPUT_REQUIRED":
            task_store.set_artifacts(task_id, artifacts)
            task_store.pause_task(
                task_id,
                question=final_message or "Team Lead requested clarification.",
                interrupt_metadata={"teamLeadTaskId": team_lead_task_id, "task_type": "development"},
            )
            _record_major_step(
                task_store,
                task_id,
                step_key="tl.requesting_user_input",
                title="Team Lead requesting user input for clarification",
                agent="team-lead",
                lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
                summary_template="Team Lead requested user input: {input_reason}; awaiting user response.",
                summary_facts={"input_reason": "ambiguous requirements"},
            )
            _append_chat_entry(
                task_store,
                task_id,
                role="COMPASS",
                text=final_message or "Team Lead requested clarification.",
                tone="input-required",
            )
            log.warn("development task awaiting input", tl_task_id=team_lead_task_id)
            log.a2a("←", "team-lead", status="input-required", tl_task_id=team_lead_task_id)
            return

        if status != "completed":
            task_store.set_artifacts(task_id, artifacts)
            task_store.fail_task(task_id, final_message)
            _record_major_step(
                task_store,
                task_id,
                step_key="compass.task_failed",
                title=f"Compass marking task failed: {final_message[:200]}",
                agent="compass",
                lifecycle_state=LIFECYCLE_FAILED,
                summary_template="Compass marked the task as failed: {failure_reason}.",
                summary_facts={"failure_reason": final_message[:500] or "development task did not complete"},
            )
            _append_chat_entry(
                task_store,
                task_id,
                role="COMPASS",
                text=final_message,
                tone="failed",
            )
            log.error("development task failed", tl_task_id=team_lead_task_id, status=status)
            log.a2a("←", "team-lead", status=status or "error", tl_task_id=team_lead_task_id)
            return

        task_store.complete_task(task_id, artifacts=artifacts, message=final_message)
        _record_major_step(
            task_store,
            task_id,
            step_key="compass.task_completed",
            title="Compass marking task completed",
            agent="compass",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Compass marked the task as completed.",
        )
        _append_chat_entry(
            task_store,
            task_id,
            role="COMPASS",
            text=final_message,
            tone="completed",
        )
        log.info(
            "development task complete",
            tl_task_id=team_lead_task_id,
            pr_url=str(dispatch_data.get("prUrl") or ""),
            branch=str(dispatch_data.get("branch") or ""),
        )
        log.a2a("←", "team-lead", status="completed", tl_task_id=team_lead_task_id)

    async def handle_message(self, message: dict) -> dict:
        import os as _os

        from framework.a2a.protocol import Artifact
        from framework.devlog import AgentLogger
        from framework.instructions import load_instructions
        from framework.runtime.adapter import get_runtime
        from framework.tools.registry import get_registry

        register_compass_tools()

        msg = message.get("message", message)
        parts = msg.get("parts") or []
        user_text = next((p.get("text", "") for p in parts if p.get("text")), "")
        meta = msg.get("metadata") or {}

        # Create task via TaskStore — task.id IS the master task_id for this workflow
        task_store = self.services.task_store
        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={
                "user_request": user_text,
                "userRequest": user_text,
                "chat_history": [],
                "progress_steps": [],
                "current_major_step": "Request received by Compass",
            },
        )
        _aid = self.definition.agent_id
        # v0.8: emit the first major step (compass.received) as the canonical
        # first row of the timeline. This matches design doc §4.2.1 step #1
        # and ensures ``deriveMajorTimeline`` sees a real ``compass.received``
        # row instead of relying on the legacy ``isLegacyCompassReceived``
        # compatibility branch in the front-end renderer.
        _record_major_step(
            task_store,
            task.id,
            step_key="compass.received",
            title="Compass receiving request",
            agent=_aid,
            lifecycle_state=LIFECYCLE_RUNNING,
            summary_template="Compass received a new request.",
        )
        _append_chat_entry(task_store, task.id, role="USER", text=user_text)

        runtime = self.services.runtime or get_runtime()
        registry = get_registry()

        # --- Classify ---
        task_type = _classify_request(user_text, runtime)
        print(f"[{_aid}] task_type={task_type!r} request={user_text[:120]!r}")

        # --- Workspace path: {ARTIFACT_ROOT}/{task_id}/
        # All agents in this workflow share the same task_id as the workspace root.
        artifact_root = _os.environ.get("ARTIFACT_ROOT", "artifacts/")
        workspace_path = _os.path.join(artifact_root, task.id)

        # --- Compass logger — writes only to its own directory ---
        log = AgentLogger(task_id=task.id, agent_name=_aid)
        log.node("handle_message", task_type=task_type, task_id=task.id,
                 request=user_text[:200])

        # --- Dispatch ---
        dispatch_data = {}
        office_request: dict[str, Any] = {}
        if task_type == "development":
            jira_key = _extract_jira_key(user_text)
            log.info("dispatching development task asynchronously", jira_key=jira_key)
            task_store.update_metadata(
                task.id,
                {
                    "task_type": "development",
                    "jira_key": jira_key,
                    "workspace_path": workspace_path,
                },
            )
            response_text = _development_start_message(jira_key)
            _record_major_step(
                task_store,
                task.id,
                step_key="compass.dispatched",
                title="Compass dispatching to Team Lead",
                agent=_aid,
                lifecycle_state=LIFECYCLE_RUNNING,
                summary_template="Compass dispatched the task to the Team Lead Agent for Jira ticket {jira_key}.",
                summary_facts={"jira_key": jira_key or "unspecified"},
            )
            _append_chat_entry(task_store, task.id, role="COMPASS", text=response_text)
            ui_update = {
                "task_id": task.id,
                "task_status": "TASK_STATE_WORKING",
                "chat_message": {
                    "role": "COMPASS",
                    "text": response_text,
                    "style": "normal",
                },
            }
            initial_response = {**task_store.get_task_dict(task.id), "ui_update": ui_update}
            worker = threading.Thread(
                target=self._complete_development_task,
                kwargs={
                    "task_id": task.id,
                    "user_text": user_text,
                    "jira_key": jira_key,
                    "workspace_path": workspace_path,
                },
                daemon=True,
                name="compass-development-dispatch",
            )
            worker.start()
            print(f"[{_aid}] dispatch started in background: jira_key={jira_key!r} taskId={task.id}")
            return initial_response

        elif task_type == "office":
            log.info("dispatching office task")
            office_request = _extract_office_request(user_text, meta)
            task_store.update_metadata(task.id, {"task_type": "office", "office_request": office_request})

            # For the organize capability, ask the dimension question BEFORE
            # the output-mode question.  Most natural-language requests
            # embed a grouping hint ("by file size", "by extension", ...)
            # but miss the dimension keyword for the default output mode
            # ("workspace" is implicit).  Putting the dimension first
            # shrinks the typical "ask output mode, then ask dimension"
            # two-round trip into a single round for organize requests.
            if (
                office_request.get("capability") == "organize"
                and not _office_dimension_resolved(user_text, office_request)
            ):
                question = _office_organize_dimension_question(user_text)
                task_store.pause_task(
                    task.id,
                    question=question,
                    interrupt_metadata={
                        "kind": "office_organize_dimension",
                        "office_request": office_request,
                        "needs_clarification": {
                            "missing": "organizeGroupBy",
                            "options": [
                                {"id": d, "label": d.replace("_", " ")}
                                for d in sorted(VALID_DIMENSIONS)
                            ],
                            "user_message": question,
                        },
                    },
                )
                _record_major_step(
                    task_store,
                    task.id,
                    step_key="compass.office_organize_dimension",
                    title="Compass asking for organize dimension",
                    agent=_aid,
                    lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
                    conditional=True,
                    summary_template=(
                        "Compass is waiting for the user to pick an "
                        "organize dimension."
                    ),
                )
                _append_chat_entry(
                    task_store,
                    task.id,
                    role="COMPASS",
                    text=question,
                    tone="input-required",
                )
                log.info(
                    "office organize task awaiting dimension",
                    source_count=len(office_request.get("source_paths", [])),
                )
                ui_update = {
                    "task_id": task.id,
                    "task_status": "TASK_STATE_INPUT_REQUIRED",
                    "chat_message": {
                        "role": "COMPASS",
                        "text": question,
                        "style": "normal",
                    },
                }
                return {"task_id": task.id, "ui_update": ui_update}

            # For non-organize office capabilities (summarize / analyze),
            # or for organize requests that already include a dimension
            # and output mode, the dimension gate is satisfied.  If the
            # output_mode is still empty, ask for it now — same path as
            # before, but it no longer pre-empts the dimension round.
            gated = _gate_output_mode_round(
                task_id=task.id,
                office_request=office_request,
                task_store=task_store,
                log=log,
                aid=_aid,
                capability_label=office_request.get("capability", "summarize"),
            )
            if gated is not None:
                return gated

            dispatch_data = _dispatch_office_request(task.id, user_text, office_request, registry, log)
            response_text = dispatch_data.get("message") or f"Office task dispatched. Status: {dispatch_data.get('status', 'unknown')}"
            office_failed = _office_dispatch_failed(dispatch_data)
            office_awaiting_input = _office_dispatch_awaiting_input(dispatch_data)
            office_status = str(dispatch_data.get("status") or "").strip().lower()
            if office_awaiting_input:
                # Office paused with a structured needs_clarification payload.
                # Promote the compass task to INPUT_REQUIRED so the UI shows a
                # "waiting" tone and the user can reply with the missing
                # information. The office task is left at INPUT_REQUIRED too —
                # the next resume will re-dispatch with the resolved values.
                clarification_payload = (
                    dispatch_data.get("needs_clarification") or {}
                ) if isinstance(dispatch_data.get("needs_clarification"), dict) else {}
                clarification_question = (
                    dispatch_data.get("question")
                    or clarification_payload.get("user_message")
                    or _office_clarification_default_question(clarification_payload)
                )
                interrupt_kind = _office_interrupt_kind(office_request, clarification_payload)
                office_session = dispatch_data.get("office_session") or {}
                session_delta = {}
                if isinstance(office_session, dict) and office_session.get("service_url"):
                    session_delta["office_session"] = dict(office_session)
                    session_delta["office_service_url"] = office_session.get("service_url", "")
                    if office_session.get("container_name"):
                        session_delta["office_container_name"] = office_session.get("container_name", "")
                if session_delta:
                    task_store.update_metadata(task.id, session_delta)
                task_store.pause_task(
                    task.id,
                    question=clarification_question,
                    interrupt_metadata={
                        "kind": interrupt_kind,
                        "office_request": office_request,
                        "needs_clarification": clarification_payload,
                    },
                )
                _record_major_step(
                    task_store,
                    task.id,
                    step_key=f"compass.{interrupt_kind}",
                    title=f"Compass asking for {interrupt_kind.replace('_', ' ')}",
                    agent=_aid,
                    lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
                    conditional=True,
                    summary_template=(
                        "Compass is waiting for the user to supply {kind}."
                    ),
                    summary_facts={"kind": interrupt_kind},
                )
                _append_chat_entry(
                    task_store,
                    task.id,
                    role="COMPASS",
                    text=clarification_question,
                    tone="input-required",
                )
                log.info(
                    "office task awaiting user input",
                    capability=office_request.get("capability", "summarize"),
                    kind=interrupt_kind,
                )
                ui_update = {
                    "task_id": task.id,
                    "task_status": "TASK_STATE_INPUT_REQUIRED",
                    "chat_message": {
                        "role": "COMPASS",
                        "text": clarification_question,
                        "style": "normal",
                    },
                }
                return {"task_id": task.id, "ui_update": ui_update}
            if office_failed:
                _record_major_step(
                    task_store,
                    task.id,
                    step_key="compass.task_failed",
                    title=f"Compass marking task failed: {response_text[:200]}",
                    agent="compass",
                    lifecycle_state=LIFECYCLE_FAILED,
                    summary_template="Compass marked the task as failed: {failure_reason}.",
                    summary_facts={"failure_reason": response_text[:500] or "office dispatch failed"},
                )
            elif office_status == "completed":
                _record_major_step(
                    task_store,
                    task.id,
                    step_key="compass.task_completed",
                    title="Compass marking task completed",
                    agent="compass",
                    lifecycle_state=LIFECYCLE_DONE,
                    summary_template="Compass marked the task as completed.",
                )
            else:
                _record_major_step(
                    task_store,
                    task.id,
                    step_key="office.delivered",
                    title="Office returned a terminal result",
                    agent="office",
                    lifecycle_state=LIFECYCLE_DONE,
                    summary_template="Office delivered the report to Compass.",
                )
            office_session = dispatch_data.get("office_session") or {}
            if isinstance(office_session, dict) and office_session.get("service_url"):
                self._ack_and_cleanup_office_session(task.id, office_session)

        else:
            # General conversational task — use a tool-free LLM call for a direct answer.
            log.info("handling as general query")
            system_prompt = load_instructions("compass")
            llm_result = runtime.run(
                user_text,
                system_prompt=system_prompt,
                timeout=120,
                disallowed_tools=["*"],
            )
            response_text = (
                llm_result.get("summary")
                or llm_result.get("raw_response")
                or "I can help you with that."
            )
            _record_major_step(
                task_store,
                task.id,
                step_key="compass.task_completed",
                title="Compass marking task completed",
                agent=_aid,
                lifecycle_state=LIFECYCLE_DONE,
                summary_template="Compass marked the task as completed.",
            )

        response_tone = "normal"
        if task_type == "office":
            office_status = str(dispatch_data.get("status") or "").strip().lower()
            if _office_dispatch_failed(dispatch_data):
                response_tone = "failed"
            elif office_status in {"completed", "success"}:
                response_tone = "completed"
        elif task_type == "general":
            response_tone = "completed"

        _append_chat_entry(task_store, task.id, role="COMPASS", text=response_text, tone=response_tone)

        log.info("task complete", response_len=len(response_text))
        office_artifact_metadata = {"agentId": _aid}
        for key in ("summary", "message", "deliveryReportPath", "workspacePath", "status"):
            value = dispatch_data.get(key)
            if value not in (None, ""):
                office_artifact_metadata[key] = value
        if office_request.get("output_mode"):
            office_artifact_metadata["outputMode"] = office_request.get("output_mode")

        artifacts = [Artifact(
            name="compass-response",
            artifact_type="text/plain",
            parts=[{"text": response_text}],
            metadata=office_artifact_metadata,
        )]
        if task_type == "office" and _office_dispatch_failed(dispatch_data):
            task_store.set_artifacts(task.id, artifacts)
            task_store.fail_task(task.id, response_text)
        else:
            task_store.complete_task(task.id, artifacts=artifacts)

        # Build UI-friendly response with ui_update for frontend rendering
        display_status = dispatch_data.get("status", "unknown") if task_type == "development" else (
            dispatch_data.get("status", "unknown") if task_type == "office" else "completed"
        )
        # Use office_failed to determine UI style since it correctly captures all failure
        # states including "no-capability", "error", "failed", and "unknown"
        ui_style = "failed" if (task_type == "office" and office_failed) else (
            "failed" if display_status in ("error", "failed", "unknown") else "normal"
        )
        current_task = task_store.get_task(task.id)
        ui_update = {
            "task_id": task.id,
            "task_status": current_task.status.state.value if current_task else task.status.state.value,
            "chat_message": {
                "role": "COMPASS",
                "text": response_text,
                "style": ui_style,
            }
        }
        return {**task_store.get_task_dict(task.id), "ui_update": ui_update}

    async def resume_task(self, task_id: str, resume_value: object) -> dict:
        from framework.a2a.protocol import Artifact
        from framework.devlog import AgentLogger
        from framework.tools.registry import get_registry

        task_store = self.services.task_store
        task = task_store.get_task(task_id)
        if task is None:
            # Unknown task id — surface a clean 404-shaped error so the
            # UI can refresh the task list instead of showing
            # "Failed to send reply to Compass".
            raise LookupError(f"Task {task_id} not found")

        current_state = getattr(
            getattr(task.status, "state", None), "value",
            str(getattr(task.status, "state", "")),
        )
        # Only INPUT_REQUIRED tasks are eligible for resume.  If a
        # concurrent poll / dispatch already moved the task forward
        # (e.g. from a race with the background worker, or a stale
        # targetTaskId after the user clicked reply on a task that
        # the SSE had already updated), tell the caller so the UI
        # can refresh instead of mutating an unrelated state.
        if current_state != "TASK_STATE_INPUT_REQUIRED":
            return {
                "error": "task_not_waiting_for_input",
                "message": (
                    f"Task {task_id} is in state {current_state}; "
                    "no resume required. Refresh the task list."
                ),
                "task_id": task_id,
                "task_state": current_state,
            }

        _append_chat_entry(task_store, task_id, role="USER", text=str(resume_value))

        metadata = task.metadata or {}
        if metadata.get("task_type") != "office":
            result = await super().resume_task(task_id, resume_value)
            resumed_task = task_store.get_task(task_id)
            if resumed_task and resumed_task.status.message:
                state_value = getattr(resumed_task.status.state, "value", str(resumed_task.status.state))
                tone = {
                    "TASK_STATE_COMPLETED": "completed",
                    "TASK_STATE_FAILED": "failed",
                    "TASK_STATE_INPUT_REQUIRED": "input-required",
                }.get(state_value, "normal")
                _append_chat_entry(
                    task_store,
                    task_id,
                    role="COMPASS",
                    text=resumed_task.status.message.text(),
                    tone=tone,
                )
            return result

        register_compass_tools()
        registry = get_registry()
        log = AgentLogger(task_id=task_id, agent_name=self.definition.agent_id)

        office_request = dict(metadata.get("office_request") or {})
        # Mirror the live ``needs_clarification`` payload from the
        # current interrupt into the office_request so the
        # dimension-resume helper can detect the
        # ``organizeCustomPlan`` approval case without re-parsing
        # the interrupt metadata.
        interrupt_needs = (
            (metadata.get("_interrupt") or {}).get("needs_clarification") or {}
        )
        if interrupt_needs and "missing" in interrupt_needs:
            office_request["_needs_clarification"] = dict(interrupt_needs)
        reply_text = str(resume_value or "").strip().lower()
        # Resolve the active interrupt kind. The most recent pause_task call
        # set ``_interrupt.kind``; older tasks may have a stale value, so we
        # also fall back to the capability-derived kind for the legacy
        # output_mode flow that pre-dated this dispatch table.
        existing_interrupt = metadata.get("_interrupt") or {}
        active_kind = str(existing_interrupt.get("kind") or "").strip()
        if not active_kind and not office_request.get("output_mode"):
            active_kind = "office_output_mode"

        # B1: user-cancel-during-wait — delegate to the unified
        # ``cancel_task`` handler so the legacy "cancel" / "abort" /
        # "stop" text shortcut and the new Cancel button share the same
        # code path.  Works in any non-terminal state (SUBMITTED /
        # WORKING / INPUT_REQUIRED) — the new method no longer requires
        # the task to be paused for input.
        if reply_text in {"cancel", "abort", "stop"}:
            cancel_reason = "cancelled by user"
            log.warn(
                "office task cancelled by user during wait; "
                "delegating to cancel_task",
                reply=reply_text, kind=active_kind,
            )
            cancel_result = await self.cancel_task(task_id, cancel_reason)
            if cancel_result.get("status") == "already_terminal":
                # Nothing to do — task already terminal.  Surface a
                # structured response so the UI refreshes instead of
                # showing "Failed to send reply to Compass".
                return {
                    "task_id": task_id,
                    "ui_update": {
                        "task_id": task_id,
                        "task_status": cancel_result.get("task_state", ""),
                        "chat_message": {
                            "role": "COMPASS",
                            "text": "This task is already closed.",
                            "style": "normal",
                        },
                    },
                }
            return {
                "task_id": task_id,
                "ui_update": {
                    "task_id": task_id,
                    "task_status": "TASK_STATE_CANCELLED",
                    "chat_message": {
                        "role": "COMPASS",
                        "text": f"Task cancelled by user: {cancel_reason}",
                        "style": "failed",
                    },
                },
            }

        # Resolve the user reply against the active interrupt kind. The
        # helper returns either an office_request update dict, or an empty
        # string ``error_question`` when the reply did not validate.
        resolution = _resolve_office_resume_reply(active_kind, str(resume_value), office_request)
        if resolution.get("error_question"):
            question = str(resolution["error_question"])
            interrupt_meta = {
                "kind": active_kind,
                "office_request": office_request,
            }
            needs_clarification = resolution.get("needs_clarification")
            if needs_clarification:
                interrupt_meta["needs_clarification"] = needs_clarification
            # Walk through WORKING so the state machine accepts the
            # INPUT_REQUIRED → INPUT_REQUIRED no-op (some re-ask flows
            # want to swap the question without re-dispatching).
            try:
                task_store.resume_task(task_id)
            except Exception:
                pass
            task_store.pause_task(
                task_id,
                question=question,
                interrupt_metadata=interrupt_meta,
            )
            _record_major_step(
                task_store,
                task_id,
                step_key=f"compass.{active_kind}",
                title=f"Compass asking for {active_kind.replace('_', ' ')}",
                agent=self.definition.agent_id,
                lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
                conditional=True,
                summary_template=f"Compass is waiting for a valid {active_kind.replace('_', ' ')}.",
            )
            _append_chat_entry(
                task_store,
                task_id,
                role="COMPASS",
                text=question,
                tone="input-required",
            )
            log.warn(
                "invalid office resume reply",
                kind=active_kind,
                reply=str(resume_value)[:100],
            )
            ui_update = {
                "task_id": task_id,
                "task_status": "TASK_STATE_INPUT_REQUIRED",
                "chat_message": {
                    "role": "COMPASS",
                    "text": question,
                    "style": "normal",
                },
            }
            return {"task_id": task_id, "ui_update": ui_update}

        # Reply validated; merge any updates into office_request.
        office_request = dict(resolution.get("office_request") or office_request)
        forwarded_resume_value = resolution.get("resume_payload", str(resume_value))
        task_store.update_metadata(task_id, {"office_request": office_request})

        # B3: cross-round output-mode gate.  When the user just answered
        # the dimension round with a dimension-only reply (or, more
        # generally, the round they were on did not actually pin an
        # output mode), the dispatcher must NOT silently default to
        # ``workspace`` and ship the task.  The init path's
        # ``_gate_output_mode_round`` already enforces this rule on
        # first contact; we re-use the same helper here so the resume
        # path stays symmetric.  This is the fix for task-115d9fb72c78.
        # The task is currently INPUT_REQUIRED (we just got a reply);
        # the gate's pause_task would no-op the state machine, so we
        # walk through WORKING first.
        try:
            task_store.resume_task(task_id)
        except Exception:
            pass
        gated = _gate_output_mode_round(
            task_id=task_id,
            office_request=office_request,
            task_store=task_store,
            log=log,
            aid=self.definition.agent_id,
            capability_label=office_request.get("capability", "summarize"),
        )
        if gated is not None:
            return gated

        # If we reach here, output_mode is set.  Derive it AFTER the
        # gate so the ``or "workspace"`` fallback is unreachable — the
        # gate would have paused the task instead of letting us fall
        # through.
        output_mode = str(office_request.get("output_mode") or "")
        # A2: Compass writes ``resuming`` on the existing user-input row before
        # the agent resumes, then transitions the same row to ``done`` with
        # the accepted output location. Re-using the same step_instance_key
        # (``compass.asking_output_mode#0``) is required so the row in the
        # timeline reflects the real lifecycle (RESUMING -> DONE) instead of
        # staying stuck in RESUMING forever. Without the close-out the row
        # has no ``ended_at`` and the UI's Time Spent keeps growing each
        # refresh (bug task-03db89946011).
        _record_major_step(
            task_store,
            task_id,
            step_key="compass.asking_output_mode",
            title="Compass resuming after output location was selected",
            agent=self.definition.agent_id,
            lifecycle_state=LIFECYCLE_RESUMING,
            summary_template="Compass is resuming after output location was selected.",
        )
        # We already walked through WORKING above (so the gate's
        # pause_task would be valid INPUT_REQUIRED -> WORKING ->
        # INPUT_REQUIRED if needed).  The state machine forbids
        # WORKING -> WORKING, so don't re-call resume_task here.
        _record_major_step(
            task_store,
            task_id,
            step_key="compass.asking_output_mode",
            title="Compass accepted output location",
            agent=self.definition.agent_id,
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Compass accepted the output location: {output_mode}.",
            summary_facts={"output_mode": output_mode},
        )
        log.info("office output mode selected", output_mode=output_mode)

        # Fire-and-forget:
        # - if an Office session already exists, forward the reply to the
        #   same waiting Office agent;
        # - otherwise this clarification happened before the first Office
        #   launch (for example output-mode or pre-dispatch dimension
        #   selection), so Compass must launch Office now.
        office_session = _office_session_from_metadata(metadata, task_id)
        has_live_office_session = bool(office_session)
        if not has_live_office_session:
            timeout_meta = metadata.get("last_child_timeout") or {}
            if timeout_meta:
                timeout_message = (
                    "The waiting Office agent timed out before the reply arrived. "
                    "This task can no longer resume on the same Office session."
                )
                task_store.fail_task(task_id, timeout_message)
                return task_store.get_task_dict(task_id)

        office_artifact_metadata = {"agentId": self.definition.agent_id, "outputMode": output_mode}

        # Seed a "dispatching" artifact so the chat pane shows progress text
        # before the background worker finishes.
        if has_live_office_session:
            dispatching_text = (
                f"Office task accepted with output mode: `{output_mode}`. "
                "Compass is forwarding the reply to the waiting Office agent now."
            )
            dispatch_title = "Compass forwarding reply to Office Agent"
            dispatch_summary = "Compass forwarded the reply to the waiting Office Agent."
            worker_target = self._resume_office_task
            worker_kwargs = {
                "task_id": task_id,
                "office_session": dict(office_session),
                "resume_value": forwarded_resume_value,
                "office_request": dict(office_request),
            }
            worker_name = "compass-office-resume"
        else:
            dispatching_text = (
                f"Office task accepted with output mode: `{output_mode}`. "
                "Compass is launching the Office agent now."
            )
            dispatch_title = "Compass dispatching to Office Agent"
            dispatch_summary = "Compass dispatched the task to the Office Agent."
            worker_target = self._complete_office_task
            worker_kwargs = {
                "task_id": task_id,
                "user_text": str(metadata.get("user_request") or ""),
                "office_request": dict(office_request),
            }
            worker_name = "compass-office-dispatch"
        _record_major_step(
            task_store,
            task_id,
            step_key="compass.dispatched",
            title=dispatch_title,
            agent=self.definition.agent_id,
            lifecycle_state=LIFECYCLE_DONE,
            summary_template=dispatch_summary,
        )
        _append_chat_entry(
            task_store,
            task_id,
            role="COMPASS",
            text=dispatching_text,
            tone="normal",
        )
        task_store.set_artifacts(
            task_id,
            [Artifact(
                name="compass-response",
                artifact_type="text/plain",
                parts=[{"text": dispatching_text}],
                metadata=office_artifact_metadata,
            )],
        )

        worker = threading.Thread(
            target=worker_target,
            kwargs=worker_kwargs,
            daemon=True,
            name=worker_name,
        )
        worker.start()
        print(
            f"[{self.definition.agent_id}] office background worker started: "
            f"task_id={task_id} output_mode={output_mode!r}"
        )

        ui_update = {
            "task_id": task_id,
            "task_status": "TASK_STATE_WORKING",
            "chat_message": {
                "role": "COMPASS",
                "text": dispatching_text,
                "style": "normal",
            },
        }
        # Return a slim task reference (id + ui_update) instead of the
        # full 17KB+ task dict.  The UI re-fetches the canonical task
        # state right after this via loadTasks(false), so embedding the
        # full dict here just doubles the bandwidth and slows down the
        # resume round-trip on slower connections.
        return {"task_id": task_id, "ui_update": ui_update}

    async def cancel_task(self, task_id: str, reason: str = "") -> dict:
        """Cancel a compass task in any non-terminal state.

        Distinct from the legacy ``reply_text in {"cancel", ...}`` branch
        in :meth:`resume_task`: this method works in any state
        (SUBMITTED / WORKING / INPUT_REQUIRED), propagates the cancel
        to the running office container over A2A when the URL is
        known, and records a structured ``compass.task_cancelled``
        major step plus a chat entry so the timeline and chat
        panel both reflect the cancellation.

        The cancel signal is delivered in three layers:
          1. **Local-truth first** — the compass ``task_store`` is
             transitioned to ``CANCELLED`` so the UI flips
             immediately, even if the office container is unreachable.
          2. **In-flight office stop** — when the dispatcher stashed
             the office launch URL on the task metadata
             (``office_service_url``), POST to the office's
             ``/tasks/{id}/cancel`` endpoint to set the
             ``threading.Event`` that the office workflow observes
             at its next node boundary.
          3. **Timeline + chat** — record the cancel row + chat
             entry so the user sees the cancellation in both panes.
        """
        from framework.a2a.client import A2AClient
        from framework.a2a.protocol import TaskState
        from framework.devlog import AgentLogger

        task_store = self.services.task_store
        task = task_store.get_task(task_id) if task_store else None
        if task is None:
            return {"error": "not_found", "task_id": task_id}

        current_state = getattr(
            getattr(task.status, "state", None), "value",
            str(getattr(task.status, "state", "")),
        )
        if current_state in {
            TaskState.COMPLETED.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            return {
                "status": "already_terminal",
                "task_id": task_id,
                "task_state": current_state,
            }

        cancel_reason = (reason or "").strip() or "cancelled by user"

        # Layer 1: local-truth first.
        cancelled = task_store.cancel_task(task_id, cancel_reason)

        # Layer 2: forward the cancel to the office container if the
        # dispatcher stashed the launch URL on the task metadata.  We
        # never block the local cancel on this — best-effort only.
        office_service_url = str(
            (task.metadata or {}).get("office_service_url") or ""
        ).strip()
        propagation: dict = {"attempted": False, "delivered": False}
        if office_service_url:
            propagation["attempted"] = True
            try:
                client = A2AClient()
                # A2AClient._http_post is synchronous (urllib) so offload
                # it to a thread to avoid blocking the event loop.
                import asyncio as _asyncio
                resp = await _asyncio.to_thread(
                    client.send_cancel,
                    office_service_url, task_id, cancel_reason,
                )
                propagation["delivered"] = bool(resp)
                propagation["response"] = resp
            except Exception as exc:  # noqa: BLE001
                propagation["error"] = str(exc)[:200]
                log_warn = AgentLogger(
                    task_id=task_id, agent_name=self.definition.agent_id
                )
                log_warn.warn(
                    "office cancel propagation failed",
                    error=str(exc)[:200],
                )

        # Layer 3: timeline + chat.
        try:
            _record_major_step(
                task_store,
                task_id,
                step_key="compass.task_cancelled",
                title=f"Compass marking task cancelled by user",
                agent=self.definition.agent_id,
                lifecycle_state=LIFECYCLE_CANCELLED,
                summary_template="Compass marked the task as cancelled by user: {cancel_reason}.",
                summary_facts={"cancel_reason": cancel_reason[:500]},
            )
        except Exception:
            pass
        try:
            _append_chat_entry(
                task_store,
                task_id,
                role="COMPASS",
                text=f"Task cancelled by user: {cancel_reason}",
                tone="failed",
            )
        except Exception:
            pass

        return {
            "status": "ok" if cancelled else "already_terminal",
            "task_id": task_id,
            "task_state": "TASK_STATE_CANCELLED",
            "cancel_reason": cancel_reason,
            "office_propagation": propagation,
        }

    def _ack_and_cleanup_office_session(
        self,
        task_id: str,
        office_session: dict[str, Any],
    ) -> None:
        from framework.launcher import get_launcher
        import urllib.request

        if not isinstance(office_session, dict):
            return

        child_task_id = str(office_session.get("task_id") or task_id).strip() or task_id
        child_service_url = str(office_session.get("service_url") or "").strip()
        child_container_name = str(office_session.get("container_name") or "").strip()
        child_agent_id = str(office_session.get("agent_id") or "office").strip() or "office"

        if child_task_id and child_service_url:
            try:
                payload = json.dumps({
                    "orchestratorTaskId": task_id,
                    "exitReason": "task_completed_success",
                }).encode("utf-8")
                request = urllib.request.Request(
                    f"{child_service_url.rstrip('/')}/tasks/{child_task_id}/ack",
                    data=payload,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10):
                    pass
            except Exception:
                pass

        if child_container_name:
            try:
                get_launcher().destroy_instance(child_agent_id, child_container_name)
            except Exception:
                pass

        task_store = self.services.task_store
        if task_store is not None:
            try:
                task_store.update_metadata(
                    task_id,
                    {
                        "office_session": {},
                        "office_service_url": "",
                        "office_container_name": "",
                    },
                )
            except Exception:
                pass

    def _finalize_office_task_result(
        self,
        *,
        task_id: str,
        office_request: dict,
        dispatch_data: dict,
    ) -> None:
        from framework.a2a.protocol import Artifact
        from framework.devlog import AgentLogger
        task_store = self.services.task_store
        if task_store.get_task(task_id) is None:
            print(f"[compass] _finalize_office_task_result: task {task_id} not found, skipping")
            return

        log = AgentLogger(task_id=task_id, agent_name=self.definition.agent_id)
        office_session = dispatch_data.get("office_session") or {}
        office_service_url = str(dispatch_data.get("office_service_url") or "").strip()
        if office_service_url or (
            isinstance(office_session, dict) and office_session.get("service_url")
        ):
            session_delta = {}
            if office_service_url:
                session_delta["office_service_url"] = office_service_url
            if isinstance(office_session, dict) and office_session.get("service_url"):
                session_delta["office_session"] = dict(office_session)
                if office_session.get("container_name"):
                    session_delta["office_container_name"] = office_session.get("container_name", "")
            if session_delta:
                try:
                    task_store.update_metadata(task_id, session_delta)
                except Exception:
                    pass

        if task_store.get_task(task_id) is None:
            log.warn("office task disappeared before finalization", task_id=task_id)
            return

        response_text = dispatch_data.get("message") or (
            f"Office task dispatched. Status: {dispatch_data.get('status', 'unknown')}"
        )
        office_failed = _office_dispatch_failed(dispatch_data)
        office_awaiting_input = _office_dispatch_awaiting_input(dispatch_data)
        office_status = str(dispatch_data.get("status") or "").strip().lower()

        office_artifact_metadata = {"agentId": self.definition.agent_id}
        for key in ("summary", "message", "deliveryReportPath", "workspacePath", "status"):
            value = dispatch_data.get(key)
            if value not in (None, ""):
                office_artifact_metadata[key] = value
        if office_request.get("output_mode"):
            office_artifact_metadata["outputMode"] = office_request.get("output_mode")

        artifacts = [Artifact(
            name="compass-response",
            artifact_type="text/plain",
            parts=[{"text": response_text}],
            metadata=office_artifact_metadata,
        )]
        if office_awaiting_input:
            # The synchronous dispatch path returned input-required; the
            # synchronous handle_message branch already handled it via
            # ``return``. This branch covers the post-resume background
            # re-dispatch path where a re-run still surfaces a clarification.
            clarification_payload = (
                dispatch_data.get("needs_clarification") or {}
            ) if isinstance(dispatch_data.get("needs_clarification"), dict) else {}
            clarification_question = (
                dispatch_data.get("question")
                or clarification_payload.get("user_message")
                or _office_clarification_default_question(clarification_payload)
            )
            interrupt_kind = _office_interrupt_kind(office_request, clarification_payload)
            task_store.set_artifacts(task_id, artifacts)
            task_store.pause_task(
                task_id,
                question=clarification_question,
                interrupt_metadata={
                    "kind": interrupt_kind,
                    "office_request": office_request,
                    "needs_clarification": clarification_payload,
                },
            )
            _record_major_step(
                task_store,
                task_id,
                step_key=f"compass.{interrupt_kind}",
                title=f"Compass asking for {interrupt_kind.replace('_', ' ')}",
                agent=self.definition.agent_id,
                lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
                conditional=True,
                summary_template="Compass is waiting for the user to supply {kind}.",
                summary_facts={"kind": interrupt_kind},
            )
            _append_chat_entry(
                task_store,
                task_id,
                role="COMPASS",
                text=clarification_question,
                tone="input-required",
            )
            log.info(
                "office task awaiting user input (resume path)",
                task_id=task_id,
                kind=interrupt_kind,
            )
            return
        if office_failed:
            task_store.set_artifacts(task_id, artifacts)
            task_store.fail_task(task_id, response_text)
        else:
            task_store.complete_task(task_id, artifacts=artifacts, message=response_text)
        if office_failed:
            _record_major_step(
                task_store,
                task_id,
                step_key="compass.task_failed",
                title=f"Compass marking task failed: {response_text[:200]}",
                agent="compass",
                lifecycle_state=LIFECYCLE_FAILED,
                summary_template="Compass marked the task as failed: {failure_reason}.",
                summary_facts={"failure_reason": response_text[:500] or "office dispatch failed"},
            )
        else:
            # A6: per design doc §13.1 A6, always fire ``compass.task_completed``
            # on the happy path so the timeline has a consistent closing row,
            # regardless of the exact office_status (completed / success /
            # warning / unknown-but-not-failed). The ``office.delivered`` row
            # was previously written in the else branch but it is already
            # emitted by Office's own report_result node when applicable, so
            # we keep the canonical closure here as ``compass.task_completed``.
            _record_major_step(
                task_store,
                task_id,
                step_key="compass.task_completed",
                title="Compass marking task completed",
                agent="compass",
                lifecycle_state=LIFECYCLE_DONE,
                summary_template="Compass marked the task as completed.",
            )
        _append_chat_entry(
            task_store,
            task_id,
            role="COMPASS",
            text=response_text,
            tone="failed" if office_failed else "completed",
        )
        if not office_awaiting_input:
            current_task = task_store.get_task(task_id)
            session = dispatch_data.get("office_session") or _office_session_from_metadata(
                current_task.metadata if current_task else {},
                task_id,
            )
            if session:
                self._ack_and_cleanup_office_session(task_id, session)
        log.info(
            "office task finalization complete",
            task_id=task_id,
            status=office_status,
            failed=office_failed,
        )

    def _resume_office_task(
        self,
        *,
        task_id: str,
        office_session: dict[str, Any],
        resume_value: Any,
        office_request: dict,
    ) -> None:
        import urllib.request

        service_url = str(office_session.get("service_url") or "").strip()
        child_task_id = str(office_session.get("task_id") or task_id).strip() or task_id
        if not service_url:
            self._finalize_office_task_result(
                task_id=task_id,
                office_request=office_request,
                dispatch_data={
                    "status": "error",
                    "message": "No live Office session is available for resume.",
                    "office_session": office_session,
                },
            )
            return

        payload = json.dumps({"input": resume_value}).encode("utf-8")
        request = urllib.request.Request(
            f"{service_url.rstrip('/')}/tasks/{child_task_id}/resume",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5400) as response:
                office_response = json.loads(response.read().decode("utf-8"))
            dispatch_data = _office_task_to_dispatch_data(office_response, office_session)
        except Exception as exc:
            dispatch_data = {
                "status": "error",
                "message": str(exc),
                "office_session": office_session,
            }

        self._finalize_office_task_result(
            task_id=task_id,
            office_request=office_request,
            dispatch_data=dispatch_data,
        )

    def _complete_office_task(
        self,
        *,
        task_id: str,
        user_text: str,
        office_request: dict,
    ) -> None:
        """Background worker: dispatch the office task and finalize task state."""
        from framework.devlog import AgentLogger
        from framework.tools.registry import get_registry

        register_compass_tools()
        task_store = self.services.task_store
        if task_store.get_task(task_id) is None:
            print(f"[compass] _complete_office_task: task {task_id} not found, skipping")
            return

        log = AgentLogger(task_id=task_id, agent_name=self.definition.agent_id)
        registry = get_registry()

        try:
            dispatch_data = _dispatch_office_request(task_id, user_text, office_request, registry, log)
        except Exception as exc:
            log.error("office dispatch raised in background worker", error=str(exc))
            print(f"[compass] _complete_office_task: dispatch raised: {exc}")
            dispatch_data = {"status": "error", "message": str(exc)}

        self._finalize_office_task_result(
            task_id=task_id,
            office_request=office_request,
            dispatch_data=dispatch_data,
        )

    async def get_task(self, task_id: str) -> dict:
        """Return real task state from TaskStore."""
        return self.services.task_store.get_task_dict(task_id)

    def serve_ui(self, path: str) -> dict:
        """Handle UI-related requests."""
        return handle_ui_request("GET", path, self.services.task_store, _log_store_url())
