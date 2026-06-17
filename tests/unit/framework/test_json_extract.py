"""Unit tests for framework.json_extract.

Covers the parser hygiene chain (``<think>`` stripping, fence stripping,
balanced-brace candidate enumeration, required-key filtering) so that the
shared extractor stays robust across copilot-cli, claude-code, and
codex-cli LLM response shapes.
"""
from __future__ import annotations

from framework.json_extract import (
    extract_first_json,
    extract_json_array,
    extract_json_object,
    strip_code_fences,
    strip_think_blocks,
)


class TestStripThinkBlocks:

    def test_removes_single_block(self):
        text = "<think>some reasoning here</think>{\"a\": 1}"
        assert strip_think_blocks(text) == '{"a": 1}'

    def test_removes_multiple_blocks(self):
        text = "<think>one</think>real{\"a\": 1}<think>two</think>"
        assert strip_think_blocks(text) == 'real{"a": 1}'

    def test_handles_multiline_block(self):
        text = "<think>\nmulti\nline\n</think>payload"
        assert strip_think_blocks(text) == "payload"

    def test_passthrough_when_no_block(self):
        assert strip_think_blocks("plain text") == "plain text"

    def test_empty_input(self):
        assert strip_think_blocks("") == ""


class TestStripCodeFences:

    def test_strips_json_fence(self):
        assert strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_strips_bare_fence(self):
        assert strip_code_fences('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_no_fence_passthrough(self):
        assert strip_code_fences('{"a": 1}') == '{"a": 1}'

    def test_only_outer_fence_stripped(self):
        text = '```json\n{"text": "see ```inner```"}\n```'
        # Inner fences should not be stripped — only the outer pair.
        stripped = strip_code_fences(text)
        assert stripped.startswith('{"text":')
        assert "```inner```" in stripped


class TestExtractJsonObject:

    def test_pure_json(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_with_think_block(self):
        text = '<think>let me think</think>{"score": 0.9, "verdict": "pass"}'
        assert extract_json_object(text) == {"score": 0.9, "verdict": "pass"}

    def test_with_markdown_fence(self):
        text = '```json\n{"score": 0.9, "verdict": "pass"}\n```'
        assert extract_json_object(text) == {"score": 0.9, "verdict": "pass"}

    def test_with_prose_preamble(self):
        text = 'Here is the assessment: {"score": 0.7, "verdict": "fail"}'
        assert extract_json_object(text) == {"score": 0.7, "verdict": "fail"}

    def test_handles_nested_objects(self):
        text = '{"outer": {"inner": {"deep": 1}}, "other": 2}'
        result = extract_json_object(text)
        assert result == {"outer": {"inner": {"deep": 1}}, "other": 2}

    def test_required_keys_picks_matching_candidate(self):
        # Two unrelated dicts in the same response; required_keys disambiguates.
        text = (
            'First object: {"unrelated": true} and the real one: '
            '{"score": 0.5, "verdict": "fail", "extra": "x"}'
        )
        result = extract_json_object(text, required_keys={"score", "verdict"})
        assert result == {"score": 0.5, "verdict": "fail", "extra": "x"}

    def test_required_keys_missing_returns_none(self):
        text = '{"only": "this"}'
        assert extract_json_object(text, required_keys={"score", "verdict"}) is None

    def test_picks_largest_when_no_required_keys(self):
        text = 'small: {"a": 1} large: {"b": 2, "c": 3, "d": 4}'
        result = extract_json_object(text)
        assert result == {"b": 2, "c": 3, "d": 4}

    def test_braces_inside_strings_not_counted(self):
        text = '{"msg": "this has { and } characters", "ok": true}'
        result = extract_json_object(text)
        assert result == {"msg": "this has { and } characters", "ok": True}

    def test_malformed_returns_none(self):
        assert extract_json_object("{not valid json}") is None

    def test_empty_returns_none(self):
        assert extract_json_object("") is None
        assert extract_json_object(None) is None  # type: ignore[arg-type]

    def test_array_only_returns_none(self):
        # extract_json_object is for objects; arrays are extracted by the
        # array variant.
        assert extract_json_object("[1, 2, 3]") is None


class TestExtractJsonArray:

    def test_pure_array(self):
        assert extract_json_array("[1, 2, 3]") == [1, 2, 3]

    def test_with_think_block(self):
        text = '<think>thinking</think>[{"severity": "high"}]'
        assert extract_json_array(text) == [{"severity": "high"}]

    def test_with_fence(self):
        text = '```json\n[{"file": "x.py", "line": 1}]\n```'
        assert extract_json_array(text) == [{"file": "x.py", "line": 1}]

    def test_with_prose(self):
        text = 'Here are issues: [{"severity": "low", "message": "z"}]'
        assert extract_json_array(text) == [{"severity": "low", "message": "z"}]

    def test_empty_array(self):
        assert extract_json_array("[]") == []

    def test_brackets_inside_strings_not_counted(self):
        text = '[{"msg": "has [ and ] characters"}]'
        assert extract_json_array(text) == [{"msg": "has [ and ] characters"}]

    def test_object_only_returns_none(self):
        assert extract_json_array('{"a": 1}') is None

    def test_malformed_returns_none(self):
        assert extract_json_array("[not valid]") is None

    def test_empty_returns_none(self):
        assert extract_json_array("") is None


class TestExtractFirstJson:

    def test_returns_object(self):
        assert extract_first_json('{"a": 1}') == {"a": 1}

    def test_returns_array(self):
        assert extract_first_json("[1, 2]") == [1, 2]

    def test_returns_larger_when_both_present(self):
        text = '{"a": 1} [{"big": "object", "with": "lots", "of": "fields"}]'
        # Array is larger after serialisation, so it wins.
        result = extract_first_json(text)
        assert isinstance(result, list)
        assert result[0]["big"] == "object"

    def test_returns_none_when_neither(self):
        assert extract_first_json("plain text") is None

    def test_strips_think_block(self):
        assert extract_first_json('<think>r</think>{"a": 1}') == {"a": 1}


class TestRealWorldShapes:
    """End-to-end regression cases observed from actual LLM responses."""

    def test_response_with_explanatory_prose_and_fence(self):
        text = (
            "Looking at the implementation, I found that the page is non-functional. "
            "Here is the assessment:\n\n"
            "```json\n"
            '{"score": 0.72, "verdict": "fail", "criteria_checks": [], '
            '"component_checks": [], "self_review_issues": [], '
            '"gaps": ["page is non-functional"], "summary": "Score: 0.72"}\n'
            "```\n\n"
            "The score reflects the visual fidelity."
        )
        result = extract_json_object(text, required_keys={"score", "verdict"})
        assert result is not None
        assert result["score"] == 0.72
        assert result["verdict"] == "fail"

    def test_response_with_think_block_then_object(self):
        text = (
            "<think>The user wants me to assess. Let me check each criterion. "
            "Criterion 1 passes. Criterion 2 fails because of state management.</think>\n\n"
            '{"score": 0.5, "verdict": "fail", "gaps": ["no state"]}'
        )
        result = extract_json_object(text, required_keys={"score", "verdict"})
        assert result == {"score": 0.5, "verdict": "fail", "gaps": ["no state"]}

    def test_two_unrelated_objects_required_keys_picks_correct(self):
        text = (
            '{"metadata": {"agent": "web-dev"}, "version": 1}\n'
            '{"score": 0.95, "verdict": "pass", "criteria_checks": []}'
        )
        result = extract_json_object(text, required_keys={"score", "verdict"})
        assert result is not None
        assert result["score"] == 0.95
