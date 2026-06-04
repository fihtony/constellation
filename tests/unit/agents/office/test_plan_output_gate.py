"""Tests for framework.office.plan_output_gate.

This file covers Task 2 and Task 3 of the office plan-output gate plan:
the three dataclasses, the resolve_output_contract helper, and the
parse_plan / parse_plan_with_status plan parsers.
"""
from __future__ import annotations

import dataclasses

import pytest

from framework.office.plan_output_gate import (
    GateEntry,
    GateReport,
    OutputContract,
    parse_plan,
    parse_plan_with_status,
    resolve_output_contract,
)


def test_gate_entry_is_frozen():
    entry = GateEntry(source_path="/a/b.txt", expected_path="files/b.txt")
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.source_path = "/c"  # type: ignore[misc]


def test_output_contract_is_frozen():
    contract = OutputContract(
        capability="organize",
        plan_path="/plan.md",
        output_root="/root",
        ancillary_allowlist=frozenset({"x"}),
        source_count=1,
        expected_plan_kind="files_organized",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        contract.capability = "summarize"  # type: ignore[misc]


def test_gate_report_is_clean_when_no_discrepancies():
    report = GateReport(
        capability="organize",
        plan_status="ok",
        planned_count=2,
        actual_count=2,
        missing=[],
        unexpected=[],
        mismatches=[],
    )
    assert report.is_clean is True


def test_gate_report_is_not_clean_when_missing():
    report = GateReport(
        capability="organize",
        plan_status="ok",
        planned_count=2,
        actual_count=1,
        missing=["files/missing.txt"],
        unexpected=[],
        mismatches=[],
    )
    assert report.is_clean is False


def test_gate_report_is_not_clean_when_tool_unavailable():
    report = GateReport(
        capability="organize",
        plan_status="ok",
        planned_count=0,
        actual_count=0,
        missing=[],
        unexpected=[],
        mismatches=[],
        tool_unavailable=True,
    )
    assert report.is_clean is False


def test_resolve_output_contract_organize(tmp_path):
    (tmp_path / "organized-output").mkdir()
    (tmp_path / "organized-output" / "files").mkdir()
    contract = resolve_output_contract(
        capability="organize",
        validated_paths=[str(tmp_path / "src")],
        output_mode="workspace",
        artifacts_dir=str(tmp_path),
    )
    assert contract.capability == "organize"
    assert contract.expected_plan_kind == "files_organized"
    assert contract.plan_path == str(tmp_path / "organized-output" / "files" / "organization-plan.md")
    assert contract.output_root == str(tmp_path / "organized-output" / "files")


def test_resolve_output_contract_summarize(tmp_path):
    contract = resolve_output_contract(
        capability="summarize",
        validated_paths=[str(tmp_path / "a.txt")],
        output_mode="workspace",
        artifacts_dir=str(tmp_path / "workspace"),
    )
    assert contract.capability == "summarize"
    assert contract.expected_plan_kind == "source_summary_mapping"
    assert contract.output_root == str(tmp_path / "workspace")
    assert contract.plan_path == str(tmp_path / "workspace" / "summary-plan.md")


def test_resolve_output_contract_analyze(tmp_path):
    contract = resolve_output_contract(
        capability="analyze",
        validated_paths=[str(tmp_path / "data.csv")],
        output_mode="workspace",
        artifacts_dir=str(tmp_path),
    )
    assert contract.capability == "analyze"
    assert contract.expected_plan_kind == "source_analysis_mapping"
    assert contract.output_root == str(tmp_path)
    assert contract.plan_path == str(tmp_path / "analysis-plan.md")


def test_resolve_output_contract_inplace_uses_target_under_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    contract = resolve_output_contract(
        capability="organize",
        validated_paths=[str(src)],
        output_mode="inplace",
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    assert contract.output_root == str(src / "organized-output" / "files")


def test_resolve_output_contract_inplace_summarize(tmp_path):
    src_parent = tmp_path / "src_parent"
    src_parent.mkdir()
    source = src_parent / "source.txt"
    source.write_text("hello")
    contract = resolve_output_contract(
        capability="summarize",
        validated_paths=[str(source)],
        output_mode="inplace",
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    assert contract.output_root == str(src_parent)
    assert contract.plan_path == str(src_parent / "summary-plan.md")


def test_resolve_output_contract_inplace_analyze(tmp_path):
    src_parent = tmp_path / "src_parent"
    src_parent.mkdir()
    source = src_parent / "data.csv"
    source.write_text("a,b\n1,2\n")
    contract = resolve_output_contract(
        capability="analyze",
        validated_paths=[str(source)],
        output_mode="inplace",
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    assert contract.output_root == str(src_parent)
    assert contract.plan_path == str(src_parent / "analysis-plan.md")


def test_resolve_output_contract_unknown_capability_raises(tmp_path):
    with pytest.raises(ValueError):
        resolve_output_contract(
            capability="not_a_capability",
            validated_paths=[str(tmp_path / "a.txt")],
            output_mode="workspace",
            artifacts_dir=str(tmp_path),
        )


def test_resolve_output_contract_inplace_requires_validated_paths(tmp_path):
    with pytest.raises(ValueError):
        resolve_output_contract(
            capability="summarize",
            validated_paths=[],
            output_mode="inplace",
            artifacts_dir=str(tmp_path),
        )


def test_resolve_output_contract_workspace_requires_artifacts_dir_or_env(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OFFICE_WORKSPACE_ROOT", raising=False)
    with pytest.raises(ValueError):
        resolve_output_contract(
            capability="summarize",
            validated_paths=[str(tmp_path / "a.txt")],
            output_mode="workspace",
            artifacts_dir="",
        )


# ---------------------------------------------------------------------------
# Task 3 — parse_plan / parse_plan_with_status
# ---------------------------------------------------------------------------


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_parse_plan_organize_extracts_pairs(tmp_path):
    plan = """# Plan
## Files Organized
| source | destination |
| --- | --- |
| /src/a.txt | files/a.txt |
| /src/b.txt | documents/b.txt |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    entries = parse_plan("organize", plan_path)
    assert len(entries) == 2
    assert entries[0].source_path == "/src/a.txt"
    assert entries[0].expected_path == "files/a.txt"
    assert entries[1].expected_path == "documents/b.txt"


def test_parse_plan_summarize_extracts_expanded_file_rows(tmp_path):
    plan = """# Plan
## Source -> Summary Mapping
| source | summary_target |
| --- | --- |
| /src/a.txt | a.md |
| /src/b.txt | b.md |
"""
    plan_path = _write(tmp_path, "summary-plan.md", plan)
    entries = parse_plan("summarize", plan_path)
    assert len(entries) == 2
    assert entries[0].extras["summary_target"] == "a.md"


def test_parse_plan_analyze_extracts_output_rows_and_committed_fields(tmp_path):
    plan = """# Plan
## Source -> Analysis Mapping
| source | analysis_target |
| --- | --- |
| /src/data.csv | data.analysis.md |

## Committed Fields
- field_count: 4
- numeric_field_count: 2
"""
    plan_path = _write(tmp_path, "analysis-plan.md", plan)
    status, _invalid, entries, committed, _error = parse_plan_with_status("analyze", plan_path)
    assert status == "ok"
    assert len(entries) == 1
    assert committed == {"field_count": 4, "numeric_field_count": 2}


def test_parse_plan_missing_returns_missing_status(tmp_path):
    status, _invalid, entries, _committed, _error = parse_plan_with_status(
        "organize", str(tmp_path / "absent.md")
    )
    assert status == "missing"
    assert entries == []


def test_parse_plan_wrong_capability_returns_invalid(tmp_path):
    plan_path = _write(
        tmp_path,
        "organization-plan.md",
        "# Plan\n## Source -> Summary Mapping\n| s | t |\n|---|---|\n| a | b |\n",
    )
    status, _invalid, _entries, _committed, error = parse_plan_with_status(
        "summarize", plan_path
    )
    assert status == "invalid"
    assert "summary" in error.lower()


def test_parse_plan_destination_with_parent_traversal_is_invalid(tmp_path):
    plan = """## Files Organized
| source | destination |
| --- | --- |
| /src/a.txt | ../escape.txt |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, invalid_entries, _entries, _committed, _error = parse_plan_with_status(
        "organize", plan_path
    )
    assert status == "invalid"
    assert any("../escape.txt" in e for e in invalid_entries)


def test_parse_plan_destination_absolute_path_is_invalid(tmp_path):
    plan = """## Files Organized
| source | destination |
| --- | --- |
| /src/a.txt | /etc/passwd |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, _invalid, _entries, _committed, _error = parse_plan_with_status(
        "organize", plan_path
    )
    assert status == "invalid"


def test_parse_plan_duplicate_rows_is_invalid(tmp_path):
    plan = """## Files Organized
| source | destination |
| --- | --- |
| /src/a.txt | files/a.txt |
| /src/a.txt | files/a.txt |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, _invalid, _entries, _committed, error = parse_plan_with_status(
        "organize", plan_path
    )
    assert status == "invalid"
    assert "duplicate" in error.lower()


def test_parse_plan_source_outside_validated_set_is_invalid(tmp_path):
    plan = """## Files Organized
| source | destination |
| --- | --- |
| /other/a.txt | files/a.txt |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, _invalid, _entries, _committed, error = parse_plan_with_status(
        "organize", plan_path, validated_source_roots=["/src"]
    )
    assert status == "invalid"
    assert "outside" in error.lower() or any("outside" in e for e in _invalid)


def test_parse_plan_folder_source_not_expanded_is_invalid(tmp_path):
    plan = """## Source -> Summary Mapping
| source | summary_target |
| --- | --- |
| /src/folder | summary.md |
"""
    plan_path = _write(tmp_path, "summary-plan.md", plan)
    status, _invalid, _entries, _committed, error = parse_plan_with_status(
        "summarize",
        plan_path,
        expanded_file_list=["/src/folder/a.txt", "/src/folder/b.txt"],
    )
    assert status == "invalid"
    assert "expand" in error.lower() or any("expand" in e for e in _invalid)


def test_parse_plan_empty_with_non_empty_inventory_is_invalid(tmp_path):
    plan = """## Files Organized
| source | destination |
| --- | --- |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, _invalid, _entries, _committed, error = parse_plan_with_status(
        "organize", plan_path, source_count=3
    )
    assert status == "invalid"
    assert "non-empty" in error.lower() or any("non-empty" in e for e in _invalid)


def test_parse_plan_huge_file_bounded_by_size_cap(tmp_path, monkeypatch):
    from framework.office import plan_output_gate as pog
    monkeypatch.setattr(pog, "_MAX_PLAN_BYTES", 16)
    plan_path = _write(tmp_path, "organization-plan.md", "x" * 64)
    status, _invalid, _entries, _committed, _error = parse_plan_with_status(
        "organize", plan_path
    )
    assert status == "unparseable"


def test_parse_plan_non_utf8_rejected(tmp_path):
    plan_path = tmp_path / "organization-plan.md"
    plan_path.write_bytes(b"\xff\xfe garbage")
    status, _invalid, _entries, _committed, _error = parse_plan_with_status(
        "organize", str(plan_path)
    )
    assert status == "unparseable"
