"""Tests for the office output_paths helper.

The helper is the single source of truth for *where* an office
deliverable should land. The two prompt builders and the verifier
all consume it so the prompt and the verifier cannot drift.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from agents.office.output_paths import (
    all_targets_for_capability,
    target_for_source,
    target_with_suffix,
)


# ---------------------------------------------------------------------------
# target_for_source
# ---------------------------------------------------------------------------


def test_target_for_source_workspace_dir_lands_in_artifacts():
    artifacts = tempfile.mkdtemp()
    result = target_for_source("workspace", "/data", artifacts, "data.analysis.md")
    assert result == os.path.join(artifacts, "data.analysis.md")


def test_target_for_source_workspace_file_lands_in_artifacts():
    artifacts = tempfile.mkdtemp()
    result = target_for_source("workspace", "/data/sales.csv", artifacts, "sales.csv.analysis.md")
    assert result == os.path.join(artifacts, "sales.csv.analysis.md")


def test_target_for_source_inplace_dir_lands_inside_source(tmp_path):
    """The deliverable must live *inside* the source directory, not as a sibling.

    This pins Bug A: the analyze prompt used to advertise
    `/data.analysis.md` (sibling) for directory inputs in inplace mode.
    """
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    result = target_for_source("inplace", str(source_dir), str(tmp_path / "artifacts"), "data.analysis.md")
    assert result == str(source_dir / "data.analysis.md")


def test_target_for_source_inplace_file_lands_next_to_source(tmp_path):
    source_file = tmp_path / "sales.csv"
    source_file.write_text("a,b\n1,2\n", encoding="utf-8")
    result = target_for_source("inplace", str(source_file), str(tmp_path / "artifacts"), "sales.csv.analysis.md")
    assert result == str(tmp_path / "sales.csv.analysis.md")


def test_target_for_source_unknown_mode_falls_back_to_workspace():
    artifacts = tempfile.mkdtemp()
    result = target_for_source("bogus", "/data/sales.csv", artifacts, "sales.csv.analysis.md")
    assert result == os.path.join(artifacts, "sales.csv.analysis.md")


# ---------------------------------------------------------------------------
# target_with_suffix
# ---------------------------------------------------------------------------


def test_target_with_suffix_uses_basename_and_suffix(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    result = target_with_suffix("inplace", str(source_dir), str(tmp_path / "artifacts"), ".analysis.md")
    assert result == str(source_dir / "data.analysis.md")


def test_target_with_suffix_strips_trailing_separators(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    result = target_with_suffix("inplace", str(source_dir) + "/", str(tmp_path / "artifacts"), ".summary.md")
    assert result == str(source_dir / "data.summary.md")


def test_target_with_suffix_empty_source_falls_back_to_output():
    result = target_with_suffix("workspace", "", "/tmp/artifacts", ".analysis.md")
    assert os.path.basename(result) == "output.analysis.md"


# ---------------------------------------------------------------------------
# all_targets_for_capability
# ---------------------------------------------------------------------------


def test_all_targets_for_capability_analyze_dir_inplace(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    expected = all_targets_for_capability("analyze", [str(source_dir)], "inplace", str(tmp_path / "artifacts"))
    assert expected == [str(source_dir / "data.analysis.md")]


def test_all_targets_for_capability_analyze_file_workspace(tmp_path):
    source_file = tmp_path / "sales.csv"
    source_file.write_text("a,b\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    expected = all_targets_for_capability("analyze", [str(source_file)], "workspace", str(artifacts))
    assert expected == [str(artifacts / "sales.csv.analysis.md")]


def test_all_targets_for_capability_summarize_single_file(tmp_path):
    source_file = tmp_path / "x.txt"
    source_file.write_text("hi")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    expected = all_targets_for_capability("summarize", [str(source_file)], "workspace", str(artifacts))
    assert expected == [str(artifacts / "x.txt.summary.md")]


def test_all_targets_for_capability_summarize_multi_file(tmp_path):
    a = tmp_path / "a.txt"; a.write_text("a")
    b = tmp_path / "b.txt"; b.write_text("b")
    artifacts = tmp_path / "artifacts"; artifacts.mkdir()
    expected = all_targets_for_capability("summarize", [str(a), str(b)], "workspace", str(artifacts))
    assert expected == [
        str(artifacts / "a.txt.summary.md"),
        str(artifacts / "b.txt.summary.md"),
        str(artifacts / "combined-summary.md"),
    ]


def test_all_targets_for_capability_organize_inplace_dir(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    expected = all_targets_for_capability("organize", [str(source_dir)], "inplace", str(tmp_path / "artifacts"))
    assert expected == [
        str(source_dir / "organization-plan.md"),
        str(source_dir / "organized-output" / "files"),
    ]


def test_all_targets_for_capability_organize_workspace(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    artifacts = tmp_path / "artifacts"; artifacts.mkdir()
    expected = all_targets_for_capability("organize", [str(source_dir)], "workspace", str(artifacts))
    assert expected == [
        str(artifacts / "organization-plan.md"),
        str(artifacts / "organized-output" / "files"),
    ]


def test_all_targets_for_capability_unknown_capability_returns_empty():
    assert all_targets_for_capability("nope", ["/data"], "inplace", "/tmp/artifacts") == []