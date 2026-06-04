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


import os as _os_for_time
import re


def test_organize_by_modified_time_buckets_by_year_month(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    p1 = _make_file(src, "a.txt", b"a")
    p2 = _make_file(src, "b.txt", b"b")
    _os_for_time.utime(p1, (1700000000, 1700000000))  # 2023-11
    _os_for_time.utime(p2, (1735689600, 1735689600))  # 2025-01
    out = tmp_path / "out"
    out.mkdir()
    result = OrganizeByModifiedTimeTool().execute_sync(source=str(src), output_root=str(out))
    assert result.success, result.error
    payload = json.loads(result.output)
    by_src = {entry["source"]: entry["destination"] for entry in payload["entries"]}
    assert by_src["a.txt"].startswith("2023-11/")
    assert by_src["b.txt"].startswith("2025-01/")


def test_organize_by_created_time_falls_back_to_mtime(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    p1 = _make_file(src, "fallback.txt", b"x")
    _os_for_time.utime(p1, (1700000000, 1700000000))  # 2023-11
    out = tmp_path / "out"
    out.mkdir()
    result = OrganizeByCreatedTimeTool().execute_sync(source=str(src), output_root=str(out))
    assert result.success, result.error
    plan_text = (out / "organization-plan.md").read_text()
    # The fallback assumption must be recorded so the reader can tell.
    assert "inferred_from" in plan_text or "fallback" in plan_text.lower()


def test_organize_by_accessed_time_buckets_by_year_month(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    p1 = _make_file(src, "a.txt", b"a")
    _os_for_time.utime(p1, (1700000000, 1700000000))
    out = tmp_path / "out"
    out.mkdir()
    result = OrganizeByAccessedTimeTool().execute_sync(source=str(src), output_root=str(out))
    assert result.success, result.error
    payload = json.loads(result.output)
    bucket = payload["entries"][0]["destination"].split("/")[0]
    assert re.match(r"^\d{4}-\d{2}$", bucket)


def test_organize_by_filename_buckets_by_first_letter(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_file(src, "alpha.txt", b"a")
    _make_file(src, "beta.txt", b"b")
    _make_file(src, "1number.txt", b"1")  # numeric first char -> _other
    out = tmp_path / "out"
    out.mkdir()
    result = OrganizeByFilenameTool().execute_sync(source=str(src), output_root=str(out))
    assert result.success, result.error
    payload = json.loads(result.output)
    by_src = {entry["source"]: entry["destination"] for entry in payload["entries"]}
    assert by_src["alpha.txt"].startswith("A/")
    assert by_src["beta.txt"].startswith("B/")
    assert by_src["1number.txt"].startswith("_other/")


def test_organize_by_filename_preserves_subdirectory(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    sub = src / "sub"
    sub.mkdir()
    _make_file(sub, "gamma.txt", b"g")
    out = tmp_path / "out"
    out.mkdir()
    result = OrganizeByFilenameTool().execute_sync(source=str(src), output_root=str(out))
    payload = json.loads(result.output)
    entry = payload["entries"][0]
    assert entry["destination"].startswith("G/")
    assert "sub/gamma.txt" in entry["destination"]
