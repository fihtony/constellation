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


def test_parse_plan_organize_accepts_relative_source_paths_within_validated_root(tmp_path):
    source_root = tmp_path / "source"
    nested = source_root / "nested"
    nested.mkdir(parents=True)
    (nested / "alpha.txt").write_text("alpha", encoding="utf-8")
    plan = """## Files Organized
| Source Path | Destination |
| --- | --- |
| nested/alpha.txt | files/by-topic/alpha.txt |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, invalid_entries, entries, _committed, error = parse_plan_with_status(
        "organize",
        plan_path,
        validated_source_roots=[str(source_root)],
        source_count=1,
    )
    assert status == "ok"
    assert invalid_entries == []
    assert error == ""
    assert len(entries) == 1
    assert entries[0].expected_path == "files/by-topic/alpha.txt"


def test_parse_plan_organize_accepts_grouped_tables_under_subheadings(tmp_path):
    source_root = tmp_path / "source"
    dated = source_root / "0103"
    dated.mkdir(parents=True)
    (dated / "2.txt").write_text("alpha", encoding="utf-8")
    plan = """# Folder Organization Plan

## Files Organized

### small/ (< 3,500 bytes)
| File | Size | Source |
| --- | --- | --- |
| 0103-2.txt | 2396 | 0103/2.txt |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, invalid_entries, entries, _committed, error = parse_plan_with_status(
        "organize",
        plan_path,
        validated_source_roots=[str(source_root)],
        source_count=1,
    )
    assert status == "ok"
    assert invalid_entries == []
    assert error == ""
    assert len(entries) == 1
    assert entries[0].source_path == "0103/2.txt"
    assert entries[0].expected_path == "small/0103-2.txt"


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


# ---------------------------------------------------------------------------
# Task 4 — walk_output, diff, run
# ---------------------------------------------------------------------------

from framework.office.plan_output_gate import walk_output, diff, run  # noqa: E402
import os  # noqa: E402


def _touch(root, *parts):
    p = root.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    return str(p.relative_to(root))


def test_walk_output_returns_basename_paths(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    _touch(tmp_path, "files", "b.txt")
    files = walk_output(str(tmp_path), allowlist={"organization-plan.md"})
    assert "files/a.txt" in files
    assert "files/b.txt" in files


def test_walk_output_excludes_ancillary_files(tmp_path):
    _touch(tmp_path, "organization-plan.md")
    _touch(tmp_path, "files", "a.txt")
    files = walk_output(str(tmp_path), allowlist={"organization-plan.md"})
    assert "organization-plan.md" not in files
    assert "files/a.txt" in files


def test_walk_output_excludes_timestamped_backups(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    _touch(tmp_path, "files", "a.txt.20260603-120000.bak")
    files = walk_output(str(tmp_path), allowlist={"organization-plan.md"})
    assert "files/a.txt" in files
    assert not any(f.endswith(".bak") for f in files)


def test_walk_output_ignores_hidden_files_and_empty_dirs(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    (tmp_path / "files" / "emptydir").mkdir()
    (tmp_path / ".hidden").write_text("h", encoding="utf-8")
    files = walk_output(str(tmp_path), allowlist={"organization-plan.md"})
    assert "files/a.txt" in files
    assert ".hidden" not in files


def test_walk_output_ignores_ancillary_files_in_subdirectories(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    _touch(tmp_path, "files", "warnings.md")
    files = walk_output(str(tmp_path), allowlist={"warnings.md"})
    assert "files/a.txt" in files
    assert "files/warnings.md" not in files


def test_walk_output_symlink_escape_treated_as_unexpected(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "leaked.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "files" / "link"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside / "leaked.txt")
    files = walk_output(str(tmp_path), allowlist=set())
    assert "files/link" in files


def test_diff_clean_tree_returns_clean_report(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    contract = OutputContract(
        capability="organize",
        plan_path="",
        output_root=str(tmp_path),
        ancillary_allowlist=frozenset({"organization-plan.md"}),
        source_count=1,
        expected_plan_kind="files_organized",
    )
    plan = [GateEntry(source_path="/src/a.txt", expected_path="files/a.txt")]
    report = diff("organize", plan, {"files/a.txt"}, contract)
    assert report.is_clean is True


def test_diff_missing_file_populates_missing(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    contract = OutputContract(
        capability="organize",
        plan_path="",
        output_root=str(tmp_path),
        ancillary_allowlist=frozenset({"organization-plan.md"}),
        source_count=2,
        expected_plan_kind="files_organized",
    )
    plan = [
        GateEntry(source_path="/src/a.txt", expected_path="files/a.txt"),
        GateEntry(source_path="/src/b.txt", expected_path="files/b.txt"),
    ]
    report = diff("organize", plan, {"files/a.txt"}, contract)
    assert "files/b.txt" in report.missing
    assert report.is_clean is False


def test_diff_unexpected_file_populates_unexpected(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    _touch(tmp_path, "files", "b.txt")
    contract = OutputContract(
        capability="organize",
        plan_path="",
        output_root=str(tmp_path),
        ancillary_allowlist=frozenset({"organization-plan.md"}),
        source_count=1,
        expected_plan_kind="files_organized",
    )
    plan = [GateEntry(source_path="/src/a.txt", expected_path="files/a.txt")]
    report = diff("organize", plan, {"files/a.txt", "files/b.txt"}, contract)
    assert "files/b.txt" in report.unexpected


def test_diff_analyze_committed_fields_mismatch(tmp_path):
    contract = OutputContract(
        capability="analyze",
        plan_path="",
        output_root=str(tmp_path),
        ancillary_allowlist=frozenset({"analysis-plan.md"}),
        source_count=1,
        expected_plan_kind="source_analysis_mapping",
    )
    plan = [
        GateEntry(
            source_path="/src/data.csv",
            expected_path="data.analysis.md",
            extras={"analysis_target": "data.analysis.md", "field_count": 5},
        )
    ]
    actual = {"data.analysis.md"}
    report = diff("analyze", plan, actual, contract, committed={"field_count": 4})
    assert any("field_count" in m for m in report.mismatches)


def test_diff_empty_plan_with_non_empty_inventory_is_invalid(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    contract = OutputContract(
        capability="organize",
        plan_path="",
        output_root=str(tmp_path),
        ancillary_allowlist=frozenset({"organization-plan.md"}),
        source_count=3,
        expected_plan_kind="files_organized",
    )
    report = diff("organize", [], {"files/a.txt"}, contract)
    assert report.plan_status == "invalid"
