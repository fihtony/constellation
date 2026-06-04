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


import os
import json

from agents.office.organize_by_dimension import (
    OrganizeBySizeTool,
    OrganizeByTypeTool,
    OrganizeByCreatedTimeTool,
    OrganizeByModifiedTimeTool,
    OrganizeByAccessedTimeTool,
    OrganizeByFilenameTool,
)


def _make_file(tmp_path, name: str, content: bytes):
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


def test_organize_by_size_buckets_quartiles(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_file(src, "tiny.txt", b"x")  # 1 B
    _make_file(src, "small.txt", b"x" * 200)  # 200 B
    _make_file(src, "medium.txt", b"x" * 1000)  # 1000 B
    _make_file(src, "big.txt", b"x" * 5000)  # 5000 B
    out = tmp_path / "out"
    out.mkdir()
    tool = OrganizeBySizeTool()
    result = tool.execute_sync(source=str(src), output_root=str(out))
    payload = json.loads(result.output)
    assert result.success, result.error
    buckets = {entry["bucket"] for entry in payload["entries"]}
    assert buckets.issubset({"small", "medium", "large"})
    assert len(payload["entries"]) == 4
    plan_text = (out / "organization-plan.md").read_text()
    assert "Size buckets" in plan_text
    # Thresholds are recorded.
    assert "small" in plan_text and "medium" in plan_text and "large" in plan_text


def test_organize_by_size_handles_empty_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    tool = OrganizeBySizeTool()
    result = tool.execute_sync(source=str(src), output_root=str(out))
    assert result.success
    payload = json.loads(result.output)
    assert payload["entries"] == []


def test_organize_by_type_buckets_by_extension(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_file(src, "doc.pdf", b"pdf")
    _make_file(src, "data.csv", b"csv")
    _make_file(src, "code.py", b"py")
    _make_file(src, "image.png", b"\x89PNG")
    out = tmp_path / "out"
    out.mkdir()
    tool = OrganizeByTypeTool()
    result = tool.execute_sync(source=str(src), output_root=str(out))
    payload = json.loads(result.output)
    assert result.success, result.error
    by_dest = {entry["source"]: entry["destination"] for entry in payload["entries"]}
    assert by_dest["doc.pdf"].startswith("documents/")
    assert by_dest["data.csv"].startswith("data/")
    assert by_dest["code.py"].startswith("code/")
    assert by_dest["image.png"].startswith("images/")
