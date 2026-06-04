"""Tests for agents/office/dimensions.py — the dimension contract."""
from __future__ import annotations

from agents.office.dimensions import (
    VALID_DIMENSIONS,
    parse_dimension,
)


def test_valid_dimensions_exact_set():
    assert VALID_DIMENSIONS == frozenset({
        "size",
        "type",
        "created_time",
        "modified_time",
        "accessed_time",
        "filename",
    })


def test_parse_dimension_from_metadata_wins():
    md = {"organizeGroupBy": "size"}
    assert parse_dimension(md, "please organize by name") == "size"


def test_parse_dimension_normalizes_case():
    md = {"organizeGroupBy": "SIZE"}
    assert parse_dimension(md, "") == "size"


def test_parse_dimension_rejects_unknown_metadata_value():
    md = {"organizeGroupBy": "alphabetical"}
    # Unknown metadata value falls through to keyword scan; no keyword -> "".
    assert parse_dimension(md, "") == ""


def test_parse_dimension_keyword_english_size():
    assert parse_dimension({}, "please organize by file size") == "size"


def test_parse_dimension_keyword_chinese_size():
    assert parse_dimension({}, "请按文件大小整理") == "size"


def test_parse_dimension_keyword_chinese_type():
    assert parse_dimension({}, "请按文件类型整理") == "type"


def test_parse_dimension_keyword_modified_time():
    assert parse_dimension({}, "please group by modified time") == "modified_time"


def test_parse_dimension_keyword_filename():
    assert parse_dimension({}, "按名称分组") == "filename"


def test_parse_dimension_keyword_created_time_chinese():
    assert parse_dimension({}, "按创建时间") == "created_time"


def test_parse_dimension_returns_empty_when_no_signal():
    assert parse_dimension({}, "please organize this folder") == ""


def test_parse_dimension_metadata_overrides_keyword():
    md = {"organizeGroupBy": "type"}
    assert parse_dimension(md, "by file size please") == "type"


def test_parse_dimension_handles_missing_metadata():
    assert parse_dimension(None, "by filename") == "filename"
    assert parse_dimension(None, "no signal here") == ""
