"""Unit tests for the natural-language output-mode scanner.

The scanner is the symmetric counterpart of
:func:`framework.office.dimensions.parse_dimension`: both validators look
at structured metadata first and then keyword-scan the user text.  When
the user says ``"in workspace"`` or ``"原地"`` Compass must resolve
``output_mode`` without triggering the clarification round-trip.

Failing this test was the proximate cause of task ``task-c42c4d765fd2``
getting stuck on the office output-mode clarification even though the
user had clearly written "in workspace" in the request.
"""

from __future__ import annotations

from agents.compass.agent import _extract_office_request, _scan_output_mode_from_text


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def test_scan_output_mode_from_text_accepts_workspace_phrases():
    assert _scan_output_mode_from_text("please put results in workspace") == "workspace"
    assert _scan_output_mode_from_text("write to workspace") == "workspace"
    assert _scan_output_mode_from_text("use workspace output") == "workspace"
    assert _scan_output_mode_from_text("workspace mode") == "workspace"
    assert _scan_output_mode_from_text("写到工作区") == "workspace"
    assert _scan_output_mode_from_text("输出到工作区") == "workspace"
    assert _scan_output_mode_from_text("工作区") == "workspace"


def test_scan_output_mode_from_text_accepts_inplace_phrases():
    assert _scan_output_mode_from_text("please write in place") == "inplace"
    assert _scan_output_mode_from_text("inplace mode please") == "inplace"
    assert _scan_output_mode_from_text("in-place output") == "inplace"
    assert _scan_output_mode_from_text("write inside the source") == "inplace"
    assert _scan_output_mode_from_text("in the source folder") == "inplace"
    assert _scan_output_mode_from_text("原地") == "inplace"
    assert _scan_output_mode_from_text("原位") == "inplace"
    assert _scan_output_mode_from_text("就地") == "inplace"
    assert _scan_output_mode_from_text("写到原文件夹") == "inplace"


def test_scan_output_mode_from_text_rejects_unrelated_text():
    """Bare ``workspace`` token in unrelated context must not match.

    The scanner only matches full phrases so it never mis-classifies
    "in the workspace" when the user means a *folder named* workspace
    rather than the output mode.  We test the conservative behaviour
    here: a standalone "workspace" word does NOT match unless it is
    part of a known phrase.
    """
    # Bare "workspace" alone is ambiguous and not in the phrase list.
    assert _scan_output_mode_from_text("the workspace folder") == ""
    # Empty / whitespace.
    assert _scan_output_mode_from_text("") == ""
    assert _scan_output_mode_from_text("   ") == ""
    # Unrelated sentence.
    assert _scan_output_mode_from_text("organize by file size") == ""
    assert _scan_output_mode_from_text("please summarize the pdf") == ""


# ---------------------------------------------------------------------------
# _extract_office_request integration
# ---------------------------------------------------------------------------


def test_extract_office_request_resolves_workspace_from_user_text():
    """The bug from task task-c42c4d765fd2: user said "in workspace" in
    the user text, but the office_request came back with an empty
    output_mode and Compass re-prompted for clarification.
    """
    office_request = _extract_office_request(
        "please organize folder in /data/2026/ by file size in workspace",
        metadata={},
    )
    assert office_request["output_mode"] == "workspace"
    assert office_request["capability"] == "organize"


def test_extract_office_request_resolves_inplace_from_user_text():
    office_request = _extract_office_request(
        "请整理 /data/2026/ 这个目录，输出到原文件夹",
        metadata={},
    )
    assert office_request["output_mode"] == "inplace"


def test_extract_office_request_metadata_overrides_user_text():
    """When the orchestrator pins output_mode in metadata, that wins
    over any user-text hint.  This matches the design where the
    metadata is the authoritative source.
    """
    office_request = _extract_office_request(
        "please write in workspace mode",
        metadata={"output_mode": "inplace"},
    )
    assert office_request["output_mode"] == "inplace"


def test_extract_office_request_empty_when_no_signal():
    """When neither metadata nor user text supply an output mode, the
    request comes back with output_mode="" so the caller can trigger
    the clarification round-trip.
    """
    office_request = _extract_office_request(
        "please organize folder in /data/2026/ by file size",
        metadata={},
    )
    assert office_request["output_mode"] == ""
