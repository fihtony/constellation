"""Tests for framework.office.plan_output_gate.

This file covers Task 2 of the office plan-output gate plan:
the three dataclasses and the resolve_output_contract helper.
"""
from __future__ import annotations

import dataclasses

import pytest

from framework.office.plan_output_gate import (
    GateEntry,
    GateReport,
    OutputContract,
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
