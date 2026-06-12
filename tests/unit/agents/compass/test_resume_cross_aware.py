"""Cross-aware resume handler: dimension round must recognize output mode phrases,
and output mode round must recognize dimension phrases.

The user reported task-51fccd6b57e1 ("please organize folder in
/.../unsorted_rw" + later reply "in place") still ended up in
workspace mode.  The compass log shows the dimension round received
"in place" as the reply, rejected it as an unknown dimension, and the
output mode intent was dropped.  When the user later re-typed a valid
dimension the output_mode round had defaulted to workspace.

The fix: make the resume handler cross-aware.  When the user is in
the dimension round and types an output mode phrase, save the output
mode and continue.  Symmetrically, when in the output mode round and
the user types a dimension phrase, save the dimension and continue.
This is a methodology fix at the resume layer, not a per-test patch.
"""

from __future__ import annotations

import pytest

from agents.compass.agent import (
    _OUTPUT_MODE_PHRASES,
    _resolve_office_resume_reply,
)


def _office_request(**overrides):
    base = {
        "capability": "organize",
        "source_paths": ["/data/x"],
        "output_mode": "",
        "organize_dimension": "",
        "organize_metadata": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_output_mode_phrases_cover_workspace_and_inplace():
    """Sanity: the phrase table lists both modes.  The cross-aware
    fix relies on this being complete.
    """
    modes = {mode for _, mode in _OUTPUT_MODE_PHRASES}
    assert "workspace" in modes
    assert "inplace" in modes


# ---------------------------------------------------------------------------
# Dimension round: cross-aware detection of output mode phrases
# ---------------------------------------------------------------------------


def test_dimension_round_recognizes_inplace_alone_and_saves_output_mode():
    """The bug from task-51fccd6b57e1: user is in dimension round and
    replies "in place".  The system MUST save output_mode=inplace and
    re-prompt for dimension (it must NOT silently drop the output
    mode intent).
    """
    office_request = _office_request()
    result = _resolve_office_resume_reply(
        kind="office_organize_dimension",
        reply="in place",
        office_request=office_request,
    )
    # Output mode is saved for the next round.  The function returns
    # a fresh dict — read the mutated office_request from the result.
    saved = result.get("office_request") or {}
    assert saved.get("output_mode") == "inplace", (
        f"output mode intent was dropped; result={result!r}"
    )
    # The dimension is still missing, so the user gets a re-prompt
    # (an error_question or a needs_clarification payload).
    assert result.get("error_question") or result.get("needs_clarification"), (
        f"expected a re-prompt for dimension; got: {result!r}"
    )


def test_dimension_round_recognizes_workspace_alone_and_saves_output_mode():
    """Symmetric case: user says "workspace" in the dimension round.
    """
    office_request = _office_request()
    result = _resolve_office_resume_reply(
        kind="office_organize_dimension",
        reply="workspace",
        office_request=office_request,
    )
    assert (result.get("office_request") or {}).get("output_mode") == "workspace"


def test_dimension_round_recognizes_combined_dimension_and_output_mode():
    """When the user types BOTH a dimension and an output mode in one
    shot (e.g. "by type in place"), the handler must save both and
    dispatch with no further questions.
    """
    office_request = _office_request()
    result = _resolve_office_resume_reply(
        kind="office_organize_dimension",
        reply="by type in place",
        office_request=office_request,
    )
    # No re-prompt: both are saved.
    assert "error_question" not in result, (
        f"combined reply should not re-prompt: {result!r}"
    )
    assert "needs_clarification" not in result
    saved = result.get("office_request") or {}
    # Output mode and dimension are both saved.
    assert saved.get("output_mode") == "inplace"
    assert saved.get("organize_dimension") == "type"
    assert saved.get("organize_metadata", {}).get("organizeGroupBy") == "type"


def test_dimension_round_dimension_alone_does_not_touch_output_mode():
    """A clean dimension reply must still resolve and leave
    output_mode empty (so the next round can ask for output mode).
    """
    office_request = _office_request()
    result = _resolve_office_resume_reply(
        kind="office_organize_dimension",
        reply="by type",
        office_request=office_request,
    )
    assert "error_question" not in result
    saved = result.get("office_request") or {}
    assert saved.get("organize_dimension") == "type"
    # Output mode untouched — the next round will ask for it.
    assert saved.get("output_mode") == ""


# ---------------------------------------------------------------------------
# Output mode round: cross-aware detection of dimension phrases
# ---------------------------------------------------------------------------


def test_output_mode_round_recognizes_dimension_alone_and_saves_dimension():
    """Symmetric case: user is in output mode round and replies
    "by type".  Save the dimension and re-prompt for output mode.
    """
    office_request = _office_request()
    result = _resolve_office_resume_reply(
        kind="office_output_mode",
        reply="by type",
        office_request=office_request,
    )
    saved = result.get("office_request") or {}
    # Dimension saved for the office executor.
    assert saved.get("organize_dimension") == "type", (
        f"dimension was dropped: {saved!r}"
    )
    assert saved.get("organize_metadata", {}).get("organizeGroupBy") == "type"
    # Output mode still empty — re-prompt expected.
    assert result.get("error_question") or result.get("needs_clarification"), (
        f"expected re-prompt for output mode; got: {result!r}"
    )


def test_output_mode_round_recognizes_combined_output_mode_and_dimension():
    """User types "in place by type" in the output mode round.  Both
    intents are saved; no re-prompt needed.
    """
    office_request = _office_request()
    result = _resolve_office_resume_reply(
        kind="office_output_mode",
        reply="in place by type",
        office_request=office_request,
    )
    assert "error_question" not in result, (
        f"combined reply should not re-prompt: {result!r}"
    )
    saved = result.get("office_request") or {}
    assert saved.get("output_mode") == "inplace"
    assert saved.get("organize_dimension") == "type"


def test_output_mode_round_workspace_only_does_not_touch_dimension():
    """A clean output mode reply must still resolve and leave the
    dimension untouched.
    """
    office_request = _office_request(organize_dimension="size")
    result = _resolve_office_resume_reply(
        kind="office_output_mode",
        reply="inplace",
        office_request=office_request,
    )
    assert "error_question" not in result
    saved = result.get("office_request") or {}
    assert saved.get("output_mode") == "inplace"
    # Pre-existing dimension preserved.
    assert saved.get("organize_dimension") == "size"


# ---------------------------------------------------------------------------
# The reported bug: full reproduction
# ---------------------------------------------------------------------------


def test_reported_bug_organize_in_place_keeps_intent_through_dimension_round():
    """Full reproduction of task-51fccd6b57e1.

    1. Compass parses the request and finds no dimension and no output mode.
    2. Compass asks for dimension (office_organize_dimension round).
    3. User replies "in place" — previously this was rejected and lost.
    4. With the fix, the output mode is saved and the user is asked
       for the dimension again.
    5. User replies "by type" — dimension is saved.
    6. Compass now has both dimension AND output mode; the output
       mode round is skipped and the task is dispatched in inplace mode.
    """
    office_request = _office_request()

    # Step 3: user replies "in place" to the dimension question.
    step3 = _resolve_office_resume_reply(
        kind="office_organize_dimension",
        reply="in place",
        office_request=office_request,
    )
    step3_saved = step3.get("office_request") or {}
    assert step3_saved.get("output_mode") == "inplace", (
        "in-place intent was dropped on the dimension round"
    )

    # Step 5: user replies "by type" (the dimension question is asked
    # again because the previous reply was "in place" only).  Note:
    # the next call re-receives the office_request with output_mode
    # still pinned to inplace (the previous round saved it).
    office_request_continued = dict(step3_saved)
    step5 = _resolve_office_resume_reply(
        kind="office_organize_dimension",
        reply="by type",
        office_request=office_request_continued,
    )
    assert "error_question" not in step5
    step5_saved = step5.get("office_request") or {}
    assert step5_saved.get("organize_dimension") == "type"

    # Step 6: with both dimension and output mode set, the office
    # task is ready to dispatch in inplace mode.
    assert step5_saved.get("output_mode") == "inplace"
    assert step5_saved.get("organize_metadata", {}).get("organizeGroupBy") == "type"
