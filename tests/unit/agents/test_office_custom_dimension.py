"""Tests for the office custom-dimension plan-then-execute path.

The user reported that "by student name" should be supported via the
LLM-driven custom path.  Office must:

1. Detect ``__custom__`` from :data:`framework.office.dimensions` and
   pull the user's natural-language hint from metadata or text.
2. Phase 1 (planning): call the LLM with sample files, parse the
   JSON plan, write ``custom-organize-plan.md``, and return
   INPUT_REQUIRED so the user can approve / modify.
3. Phase 2 (execution): with the approved plan, classify the
   remaining files via LLM and materialize the bucket layout.

These tests use a fake runtime that returns deterministic JSON so the
LLM call does not have to be live.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

from framework.agent import AgentServices
from framework.office.dimensions import CUSTOM_DIMENSION
from framework.task_store import InMemoryTaskStore


def _runtime_with_plan_response(plan: dict, mapping: dict | None = None):
    """Build a fake runtime that responds with a planning JSON, then
    an execution JSON.

    The first call returns ``plan``; the second call (if any) returns
    ``{"mapping": mapping}``.  The runtime records every prompt so the
    test can assert on what was sent to the LLM.  When
    ``mapping`` is the only payload, every call returns the
    mapping (the office execution path calls the LLM once).
    """
    runtime = MagicMock()
    runtime.run = MagicMock()
    prompts: list[str] = []

    def _run(prompt, **kwargs):
        prompts.append(prompt)
        if mapping is None and len(prompts) == 1:
            return {
                "summary": json.dumps(plan),
                "raw_response": json.dumps(plan),
            }
        return {
            "summary": json.dumps({"mapping": mapping or {}}),
            "raw_response": json.dumps({"mapping": mapping or {}}),
        }

    runtime.run.side_effect = _run
    runtime.prompts = prompts
    return runtime


def _make_task_store():
    return InMemoryTaskStore()


def _organize_state(
    *,
    source: str,
    artifacts_dir: str,
    metadata: dict | None = None,
    user_text: str = "please organize by student name",
    approved_plan: dict | None = None,
):
    return {
        "capability": "organize",
        "organize_dimension": CUSTOM_DIMENSION,
        "validated_paths": [source],
        "output_mode": "workspace",
        "artifacts_dir": artifacts_dir,
        "workspace_root": artifacts_dir,
        "_message_metadata": metadata or {},
        "user_request": user_text,
        "organize_custom_plan": approved_plan or {},
    }


# ---------------------------------------------------------------------------
# Phase 1: planning
# ---------------------------------------------------------------------------


def test_planning_phase_writes_plan_and_pauses_for_approval(tmp_path):
    """Phase 1: with no approved plan, the office path calls the
    LLM, parses the JSON, writes the plan markdown, and returns
    INPUT_REQUIRED-shaped state.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "alice_intro.txt").write_text("Student: Alice\nGrade: A\n")
    (src / "bob_intro.txt").write_text("Student: Bob\nGrade: B\n")
    (src / "carol_intro.txt").write_text("Student: Carol\nGrade: C\n")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    plan = {
        "buckets": ["Alice", "Bob", "Carol"],
        "sample_mapping": {
            "alice_intro.txt": "Alice",
            "bob_intro.txt": "Bob",
            "carol_intro.txt": "Carol",
        },
        "classification_rule": (
            "Read the first line of each file. The line that starts "
            "with 'Student:' carries the bucket name."
        ),
        "rationale": (
            "Each file has a Student: header. Grouping by student "
            "produces three clean buckets."
        ),
    }
    runtime = _runtime_with_plan_response(plan)
    state = _organize_state(source=str(src), artifacts_dir=str(artifacts))

    result = execute_office_work(state | {"_runtime": runtime})

    # The LLM was called once (the planning call).
    assert len(runtime.prompts) == 1, "should call LLM exactly once for planning"
    # The plan markdown is on disk.
    plan_path = artifacts / "organized-output" / "files" / "custom-organize-plan.md"
    assert plan_path.exists(), f"plan markdown not written at {plan_path}"
    plan_text = plan_path.read_text()
    assert "Alice" in plan_text and "Bob" in plan_text and "Carol" in plan_text
    assert "Classification rule" in plan_text
    # The office returns an INPUT_REQUIRED-shaped payload with the plan.
    assert result["status"] == "input-required"
    assert result["success"] is False
    needs = result["needs_clarification"]
    assert needs["missing"] == "organizeCustomPlan"
    assert needs["plan"] == plan
    assert needs["custom_hint"] == "student name"
    assert "approve" in needs["user_message"].lower()


def test_planning_phase_passes_hint_to_llm(tmp_path):
    """The planning prompt must surface the user's custom hint so
    the LLM knows what grouping to propose.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    plan = {
        "buckets": ["X"],
        "sample_mapping": {"a.txt": "X"},
        "classification_rule": "rule",
        "rationale": "r",
    }
    runtime = _runtime_with_plan_response(plan)
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        metadata={"customDimensionHint": "subject area"},
        user_text="please organize by subject area",
    )

    result = execute_office_work(state | {"_runtime": runtime})
    assert result["status"] == "input-required"
    # The hint reached the LLM prompt.
    prompt = runtime.prompts[0]
    assert "subject area" in prompt


def test_planning_phase_falls_back_to_text_when_metadata_missing(tmp_path):
    """If the user did not pin a customDimensionHint in metadata,
    office must extract it from the user text via
    :func:`framework.office.dimensions.extract_custom_dimension_hint`.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    plan = {
        "buckets": ["X"],
        "sample_mapping": {"a.txt": "X"},
        "classification_rule": "rule",
        "rationale": "r",
    }
    runtime = _runtime_with_plan_response(plan)
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        metadata={},
        user_text="please organize by department",
    )

    result = execute_office_work(state | {"_runtime": runtime})
    assert result["status"] == "input-required"
    assert "department" in runtime.prompts[0]


def test_planning_phase_parses_json_after_reasoning_block(tmp_path):
    """Some backends emit reasoning text with JSON examples before the final JSON."""
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "alice.txt").write_text("Student: Alice\nJanuary work\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    raw_response = """
<think>
The response must follow this schema:
{
  "buckets": ["name1", "name2"],
  "sample_mapping": {"<sample_path>": "<bucket_name>"},
  "classification_rule": "rule",
  "rationale": "why"
}
</think>

{
  "buckets": ["Alice/January"],
  "sample_mapping": {"alice.txt": "Alice/January"},
  "classification_rule": "Read the Student marker and month.",
  "rationale": "Group by student, then month."
}
"""
    runtime = MagicMock()
    runtime.run = MagicMock(return_value={"summary": raw_response, "raw_response": raw_response})
    runtime.prompts = []
    state = _organize_state(source=str(src), artifacts_dir=str(artifacts))

    result = execute_office_work(state | {"_runtime": runtime})

    assert result["status"] == "input-required"
    needs = result["needs_clarification"]
    assert needs["plan"]["buckets"] == ["Alice/January"]
    assert needs["plan"]["sample_mapping"] == {"alice.txt": "Alice/January"}


def test_planning_phase_recovers_complete_plan_when_final_json_is_truncated(tmp_path):
    """If the final object is truncated, recover the last complete plan object."""
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "alice.txt").write_text("Student: Alice\nJanuary work\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    raw_response = """
<think>
Schema example:
{
  "buckets": ["name1", "name2"],
  "sample_mapping": {"<sample_path>": "<bucket_name>"},
  "classification_rule": "rule",
  "rationale": "why"
}

Thus final answer:
{
  "buckets": ["Alice/January"],
  "sample_mapping": {"alice.txt": "Alice/January"},
  "classification_rule": "Read the Student marker and month.",
  "rationale": "Group by student, then month."
}
</think>

{
  "buckets": ["Alice/January"],
  "sample_mapping": {"alice.txt": "Alice/January"},
  "classification_rule": "Read the Student marker and month.",
  "rationale": "Group by
"""
    runtime = MagicMock()
    runtime.run = MagicMock(return_value={"summary": raw_response, "raw_response": raw_response})
    state = _organize_state(source=str(src), artifacts_dir=str(artifacts))

    result = execute_office_work(state | {"_runtime": runtime})

    assert result["status"] == "input-required"
    needs = result["needs_clarification"]
    assert needs["plan"]["buckets"] == ["Alice/January"]
    assert needs["plan"]["sample_mapping"] == {"alice.txt": "Alice/January"}


def test_custom_planner_samples_across_full_tree(tmp_path):
    """Sampling must cover the source distribution, not only the first folders."""
    from agents.office.organize_by_dimension import _read_sample_files

    src = tmp_path / "src"
    for index in range(10):
        folder = src / f"{index:02d}"
        folder.mkdir(parents=True)
        (folder / "item.txt").write_text(f"bucket marker {index}\n", encoding="utf-8")

    samples = _read_sample_files(str(src), max_files=5, max_chars=80)
    sampled_paths = [item["path"] for item in samples]

    assert len(sampled_paths) == 5
    assert "00/item.txt" in sampled_paths
    assert "09/item.txt" in sampled_paths
    assert not sampled_paths == [f"{index:02d}/item.txt" for index in range(5)]


# ---------------------------------------------------------------------------
# Phase 2: execution
# ---------------------------------------------------------------------------


def test_execution_phase_copies_files_into_buckets(tmp_path):
    """Phase 2: with an approved plan, office must classify
    remaining files via LLM and materialize the bucket layout.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "alice_intro.txt").write_text("Student: Alice\n")
    (src / "bob_intro.txt").write_text("Student: Bob\n")
    (src / "carol_intro.txt").write_text("Student: Carol\n")
    (src / "dave_intro.txt").write_text("Student: Dave\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    approved_plan = {
        "buckets": ["Alice", "Bob", "Carol", "Dave"],
        "sample_mapping": {
            "alice_intro.txt": "Alice",
            "bob_intro.txt": "Bob",
            "carol_intro.txt": "Carol",
        },
        "classification_rule": "Read the first line; bucket is the Student: name.",
        "rationale": "trivial",
    }
    # LLM classifies the unsampled file (dave_intro.txt).
    runtime = _runtime_with_plan_response(
        plan=approved_plan,
        mapping={"dave_intro.txt": "Dave"},
    )
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        approved_plan=approved_plan,
    )

    result = execute_office_work(state | {"_runtime": runtime})

    # LLM was called twice: planning is a no-op here (approved plan
    # supplied), but execution ran.
    assert len(runtime.prompts) == 1
    assert result["status"] == "completed"
    assert result["success"] is True

    # All 4 files materialized into the right bucket.
    output_root = artifacts / "organized-output" / "files"
    for fname, bucket in [
        ("alice_intro.txt", "Alice"),
        ("bob_intro.txt", "Bob"),
        ("carol_intro.txt", "Carol"),
        ("dave_intro.txt", "Dave"),
    ]:
        target = output_root / bucket / fname
        assert target.exists(), f"missing materialized file: {target}"
        assert target.read_text() == (src / fname).read_text()

    # The final organization plan is on disk.
    plan_path = artifacts / "organization-plan.md"
    assert plan_path.exists()
    plan_text = plan_path.read_text()
    assert "Alice" in plan_text
    assert "Bob" in plan_text
    assert "Dave" in plan_text


def test_modify_phase_replans_with_revision_note(tmp_path):
    """A ``modify: ...`` reply must re-run the planner with the user's
    revision note and return a fresh plan for approval instead of
    immediately executing the stale plan.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("Student: Alice\nMonth: January\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    previous_plan = {
        "buckets": ["Alice-January"],
        "sample_mapping": {"a.txt": "Alice-January"},
        "classification_rule": "Combine student and month.",
        "rationale": "original",
    }
    revised_plan = {
        "buckets": ["Alice/January"],
        "sample_mapping": {"a.txt": "Alice/January"},
        "classification_rule": "Group by student, then month.",
        "rationale": "revised",
    }
    runtime = _runtime_with_plan_response(revised_plan)
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        approved_plan=previous_plan,
    ) | {
        "organize_custom_action": "modify",
        "organize_custom_modify_note": (
            "modify: create a student folder first, then month folders inside it"
        ),
    }

    result = execute_office_work(state | {"_runtime": runtime})

    assert result["status"] == "input-required"
    needs = result["needs_clarification"]
    assert needs["missing"] == "organizeCustomPlan"
    assert needs["plan"] == revised_plan
    prompt = runtime.prompts[0]
    assert "student folder first" in prompt
    assert "Alice-January" in prompt


# ---------------------------------------------------------------------------
# Phase 2: nested-bucket execution
# ---------------------------------------------------------------------------


def test_execution_phase_materializes_nested_bucket_paths(tmp_path):
    """Phase 2 must materialize the approved plan's bucket names
    verbatim.  When the plan expresses a two-level layout such as
    ``Yan/January`` (student name / month), the executor must create
    ``<output>/Yan/January/...`` and not collapse the slash into a
    single ``<output>/Yan_January/...`` directory.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "yan_jan.txt").write_text(">>> Student Yan\nJanuary essay\n")
    (src / "liam_jan.txt").write_text(">>> Student Liam\nJanuary essay\n")
    (src / "ethan_jan.txt").write_text(">>> Student Ethan\nJanuary essay\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    approved_plan = {
        "buckets": ["Yan/January", "Liam/January", "Ethan/January"],
        "sample_mapping": {
            "yan_jan.txt": "Yan/January",
            "liam_jan.txt": "Liam/January",
        },
        "classification_rule": "Group by student, then month, with a slash.",
        "rationale": "Two-level hierarchy: student / month.",
    }
    runtime = _runtime_with_plan_response(
        plan=approved_plan,
        mapping={"ethan_jan.txt": "Ethan/January"},
    )
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        approved_plan=approved_plan,
    )

    result = execute_office_work(state | {"_runtime": runtime})

    assert result["status"] == "completed"
    assert result["success"] is True

    output_root = artifacts / "organized-output" / "files"
    # Nested directory layout must exist for every bucket.
    for fname, bucket in [
        ("yan_jan.txt", "Yan/January"),
        ("liam_jan.txt", "Liam/January"),
        ("ethan_jan.txt", "Ethan/January"),
    ]:
        target = output_root / bucket / fname
        assert target.exists(), (
            f"missing nested materialized file: {target}  "
            f"(bucket {bucket!r} was collapsed instead of nested)"
        )
    # The collapsed single-segment layout must NOT exist.
    assert not (output_root / "Yan_January").exists(), (
        "bucket slash was collapsed into an underscore; "
        "the executor ignored the nested path from the approved plan"
    )

    # The final organization plan must report the nested destination.
    plan_text = (artifacts / "organization-plan.md").read_text()
    assert "Yan/January" in plan_text
    # The destination column must show the nested path, not the
    # sanitized single-segment form.
    assert "Yan_January/" not in plan_text


def test_execution_phase_rejects_incomplete_classifier_mapping(tmp_path):
    """The custom executor must not silently bucket missing mappings as unmatched.

    A sparse classifier response is a workflow-quality failure: the agent has
    not parsed enough data to execute an approved custom plan. It should stop
    before materializing an ``unmatched/`` catch-all tree.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "0103").mkdir()
    (src / "0103" / "1.txt").write_text("Student: Alice\nJanuary work\n")
    (src / "0207").mkdir()
    (src / "0207" / "1.txt").write_text("Student: Bob\nFebruary work\n")
    (src / "0307").mkdir()
    (src / "0307" / "1.txt").write_text("Student: Carol\nMarch work\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    approved_plan = {
        "buckets": ["Alice/January", "Bob/February", "Carol/March"],
        "sample_mapping": {"0103/1.txt": "Alice/January"},
        "classification_rule": "Read each file and map every file to Student/Month.",
        "rationale": "Every source file should have a student and month.",
    }
    runtime = _runtime_with_plan_response(
        plan=approved_plan,
        mapping={},  # broken classifier response: it omitted every remaining file
    )
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        approved_plan=approved_plan,
    )

    result = execute_office_work(state | {"_runtime": runtime})

    assert result["status"] == "input-required"
    assert result["success"] is False
    assert "did not classify" in result["summary"]
    output_root = artifacts / "organized-output" / "files"
    assert not (output_root / "unmatched").exists()


def test_execution_phase_accepts_absolute_sample_mapping_paths(tmp_path):
    """Planner output may use absolute source paths; execution canonicalizes them."""
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "0103").mkdir()
    (src / "0103" / "1.txt").write_text("Student: Alice\nJanuary work\n")
    (src / "0207").mkdir()
    (src / "0207" / "1.txt").write_text("Student: Bob\nFebruary work\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    approved_plan = {
        "buckets": ["Alice/January", "Bob/February"],
        "sample_mapping": {str(src / "0103" / "1.txt"): "Alice/January"},
        "classification_rule": "Read each file and map every file to Student/Month.",
        "rationale": "Planner used an absolute sample path.",
    }
    runtime = _runtime_with_plan_response(
        plan=approved_plan,
        mapping={"0207/1.txt": "Bob/February"},
    )
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        approved_plan=approved_plan,
    )

    result = execute_office_work(state | {"_runtime": runtime})

    assert result["status"] == "completed"
    assert result["success"] is True
    output_root = artifacts / "organized-output" / "files"
    assert (output_root / "Alice" / "January" / "0103" / "1.txt").exists()
    assert (output_root / "Bob" / "February" / "0207" / "1.txt").exists()


def test_execution_phase_accepts_absolute_classifier_mapping_paths(tmp_path):
    """Classifier output may also use absolute source paths."""
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "0103").mkdir()
    (src / "0103" / "1.txt").write_text("Student: Alice\nJanuary work\n")
    (src / "0207").mkdir()
    (src / "0207" / "1.txt").write_text("Student: Bob\nFebruary work\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    approved_plan = {
        "buckets": ["Alice/January", "Bob/February"],
        "sample_mapping": {"0103/1.txt": "Alice/January"},
        "classification_rule": "Read each file and map every file to Student/Month.",
        "rationale": "Classifier may echo absolute source paths.",
    }
    runtime = _runtime_with_plan_response(
        plan=approved_plan,
        mapping={str(src / "0207" / "1.txt"): "Bob/February"},
    )
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        approved_plan=approved_plan,
    )

    result = execute_office_work(state | {"_runtime": runtime})

    assert result["status"] == "completed"
    assert result["success"] is True
    output_root = artifacts / "organized-output" / "files"
    assert (output_root / "Alice" / "January" / "0103" / "1.txt").exists()
    assert (output_root / "Bob" / "February" / "0207" / "1.txt").exists()


def test_execution_phase_parses_classifier_mapping_after_reasoning_block(tmp_path):
    """Execution mapping parsing must tolerate reasoning wrappers too."""
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "0103").mkdir()
    (src / "0103" / "1.txt").write_text("Student: Alice\nJanuary work\n")
    (src / "0207").mkdir()
    (src / "0207" / "1.txt").write_text("Student: Bob\nFebruary work\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    approved_plan = {
        "buckets": ["Alice/January", "Bob/February"],
        "sample_mapping": {"0103/1.txt": "Alice/January"},
        "classification_rule": "Read each file and map every file to Student/Month.",
        "rationale": "Classifier output may include a reasoning block.",
    }
    raw_mapping = """
<think>
Use this schema:
{"mapping": {"<file_path>": "<bucket_name>"}}
</think>

{"mapping": {"0207/1.txt": "Bob/February"}}
"""
    runtime = MagicMock()
    runtime.run = MagicMock(return_value={"summary": raw_mapping, "raw_response": raw_mapping})
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        approved_plan=approved_plan,
    )

    result = execute_office_work(state | {"_runtime": runtime})

    assert result["status"] == "completed"
    assert result["success"] is True
    output_root = artifacts / "organized-output" / "files"
    assert (output_root / "Alice" / "January" / "0103" / "1.txt").exists()
    assert (output_root / "Bob" / "February" / "0207" / "1.txt").exists()


def test_execution_phase_retries_when_classifier_overuses_unmatched(tmp_path):
    """Custom bucket examples are not a closed enum; retry excessive unmatched output."""
    from agents.office.nodes import execute_office_work

    src = tmp_path / "src"
    src.mkdir()
    (src / "0103").mkdir()
    (src / "0103" / "1.txt").write_text("Student: Alice\nJanuary work\n")
    for index in range(1, 6):
        folder = src / f"02{index:02d}"
        folder.mkdir()
        (folder / "1.txt").write_text(f"Student: Student{index}\nFebruary work\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    approved_plan = {
        "buckets": ["Alice/January"],
        "sample_mapping": {"0103/1.txt": "Alice/January"},
        "classification_rule": "Read each file and map every file to Student/Month.",
        "rationale": "Bucket examples are not exhaustive.",
    }
    first_mapping = {
        f"02{index:02d}/1.txt": "__unmatched__"
        for index in range(1, 6)
    }
    retry_mapping = {
        f"02{index:02d}/1.txt": f"Student{index}/February"
        for index in range(1, 6)
    }
    responses = [
        {"summary": json.dumps({"mapping": first_mapping}), "raw_response": json.dumps({"mapping": first_mapping})},
        {"summary": json.dumps({"mapping": retry_mapping}), "raw_response": json.dumps({"mapping": retry_mapping})},
    ]
    runtime = MagicMock()
    runtime.run = MagicMock(side_effect=responses)
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        approved_plan=approved_plan,
    )

    result = execute_office_work(state | {"_runtime": runtime})

    assert result["status"] == "completed"
    assert result["success"] is True
    assert runtime.run.call_count == 2
    retry_prompt = runtime.run.call_args_list[1].args[0]
    assert "create a new bucket" in retry_prompt
    output_root = artifacts / "organized-output" / "files"
    for index in range(1, 6):
        assert (output_root / f"Student{index}" / "February" / f"02{index:02d}" / "1.txt").exists()


def test_safe_path_segment_preserves_nested_paths():
    """The path sanitizer used by the custom-dimension executor must
    treat ``/`` as a path separator, not a character to flatten.
    A single-segment name still goes through the same sanitizer so
    existing plans keep working.
    """
    from agents.office.office_tools import _safe_path_segment

    # Nested paths are preserved.
    assert _safe_path_segment("Yan/January") == "Yan/January"
    assert _safe_path_segment("Ethan/April/2026") == "Ethan/April/2026"
    # Whitespace inside a segment is collapsed to underscore, but the
    # hierarchy stays.
    assert _safe_path_segment("Yan / January") == "Yan/January"
    # Backslashes also count as path separators (Windows-friendly).
    assert _safe_path_segment(r"Ethan\January") == "Ethan/January"
    # A single-segment name without slashes is still sanitized as
    # before — dots survive and trailing underscores are stripped
    # (matches the original behaviour for the built-in dimensions).
    assert _safe_path_segment("v1.2") == "v1.2"
    # Empty / whitespace input falls back to ``"unknown"`` so the
    # executor never writes into the output root by accident.
    assert _safe_path_segment("") == "unknown"
    assert _safe_path_segment("   ") == "unknown"
    # Leading / trailing slashes do not produce empty segments.
    assert _safe_path_segment("/Yan/January/") == "Yan/January"


# ---------------------------------------------------------------------------
# Inplace mode — task-afc50de4fa71 regression
#
# The user reported two symptoms when the custom-dimension path ran
# in inplace mode on a folder like ``tests/data/2026_rw``:
#
# 1. The original sub-folders (``0103/``, ``0207/``, ...) were left
#    in place next to the new bucket tree.  The custom path was
#    using ``shutil.copy2`` instead of ``shutil.move`` and never
#    called ``_integrity_cleanup_empty_dirs`` after the transfer.
#
# 2. An ``unmatched/custom-organize-plan.md`` showed up under the
#    new layout.  The custom-dimension planner writes
#    ``custom-organize-plan.md`` to the source root during the
#    pause-for-approval phase; on the second dispatch the execute
#    phase's ``collect_organize_file_inventory`` walk swept that
#    plan in along with the user's files, the LLM classifier put
#    it under ``__unmatched__``, and the executor copied it into
#    ``unmatched/``.  Tool-produced files must be excluded from
#    the inventory walk and from the integrity verifier's
#    "unexpected" sweep.
#
# The tests below lock the new contract for both symptoms.
# ---------------------------------------------------------------------------


def _organize_state_inplace(
    *,
    source: str,
    artifacts_dir: str,
    approved_plan: dict | None = None,
    user_text: str = "please organize by student by month",
):
    return {
        "capability": "organize",
        "organize_dimension": CUSTOM_DIMENSION,
        "validated_paths": [source],
        "output_mode": "inplace",
        "artifacts_dir": artifacts_dir,
        "workspace_root": artifacts_dir,
        "_message_metadata": {},
        "user_request": user_text,
        "organize_custom_plan": approved_plan or {},
    }


def test_custom_dimension_inplace_moves_files_and_clears_originals(
    tmp_path,
):
    """Headline regression for the "original sub-folders still
    there" symptom in task-afc50de4fa71.  In inplace mode the
    custom-dimension executor must MOVE every user file (not
    copy it) and then rmdir the now-empty original sub-folders,
    so the user is left with a clean bucket tree.

    Without the fix the user would see both the original
    ``0103/``/``0207/``/... layout AND the new ``Ethan/January/...``
    layout side by side.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "messy"
    src.mkdir()
    # Lay out two date sub-folders like the real 2026_rw fixture.
    (src / "0103").mkdir()
    (src / "0103" / "1.txt").write_text("Student: Alice\nJanuary work\n")
    (src / "0207").mkdir()
    (src / "0207" / "1.txt").write_text("Student: Bob\nFebruary work\n")

    approved_plan = {
        "buckets": ["Alice/January", "Bob/February"],
        "sample_mapping": {
            "0103/1.txt": "Alice/January",
        },
        "classification_rule": "Read the first line; bucket is Student/January-or-February.",
        "rationale": "trivial",
    }
    runtime = _runtime_with_plan_response(
        plan=approved_plan,
        mapping={"0207/1.txt": "Bob/February"},
    )
    state = _organize_state_inplace(
        source=str(src),
        artifacts_dir=str(tmp_path / "artifacts"),
        approved_plan=approved_plan,
    )

    result = execute_office_work(state | {"_runtime": runtime})
    assert result["status"] == "completed", (
        f"inplace custom-dimension must succeed; got {result!r}"
    )
    assert result["success"] is True

    # Files were MOVED (not copied) into the bucket layout.  The
    # original sub-folders are gone.
    assert (src / "Alice" / "January" / "0103" / "1.txt").is_file(), (
        "user file must end up under the bucket path"
    )
    assert (src / "Bob" / "February" / "0207" / "1.txt").is_file()

    # The original sub-folders have been cleaned up — no forest
    # of stale ``0103/``/``0207/`` directories left next to the
    # new bucket tree.  This is the part the user complained
    # about.
    assert not (src / "0103").exists(), (
        "original 0103/ sub-folder must be rmdir'd after the move; "
        "the user is left with stale directory skeletons otherwise"
    )
    assert not (src / "0207").exists(), (
        "original 0207/ sub-folder must be rmdir'd after the move"
    )

    # Disk usage was not doubled: no file exists at the original
    # path any more.
    assert not (src / "0103" / "1.txt").exists(), (
        "source file must be moved, not left in place"
    )
    assert not (src / "0207" / "1.txt").exists()


def test_custom_dimension_inplace_does_not_treat_plan_as_user_file(
    tmp_path,
):
    """Headline regression for the ``unmatched/custom-organize-plan.md``
    symptom.  The custom-dimension planner writes
    ``<source>/custom-organize-plan.md`` during the pause-for-approval
    phase.  On the execute dispatch, the inventory walk must skip
    that plan (it is a tool-produced artifact, not a user file), and
    the post-run integrity verifier must skip it via the
    ``produced_paths`` allowlist.  Otherwise the executor's own plan
    ends up classified as ``__unmatched__`` and copied into a fresh
    ``unmatched/`` bucket — exactly the artifact the user saw in
    task-afc50de4fa71.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "messy"
    src.mkdir()
    (src / "0103" / "1.txt").parent.mkdir(parents=True)
    (src / "0103" / "1.txt").write_text("Student: Alice\nJanuary work\n")
    # Add a second, unsampled file so the executor is forced to
    # call the LLM classifier — that lets us assert the classifier
    # never saw the tool-produced plan in its prompt.
    (src / "0207" / "1.txt").parent.mkdir(parents=True)
    (src / "0207" / "1.txt").write_text("Student: Bob\nFebruary work\n")
    # Simulate the planner having written its draft at the source
    # root, exactly as ``_plan_published`` does in Phase 1.
    (src / "custom-organize-plan.md").write_text(
        "# Custom Organize Plan\n\nBuckets: Alice/January\n"
    )

    approved_plan = {
        "buckets": ["Alice/January", "Bob/February"],
        "sample_mapping": {"0103/1.txt": "Alice/January"},
        "classification_rule": "trivial",
        "rationale": "trivial",
    }
    runtime = _runtime_with_plan_response(
        plan=approved_plan,
        mapping={"0207/1.txt": "Bob/February"},
    )
    state = _organize_state_inplace(
        source=str(src),
        artifacts_dir=str(tmp_path / "artifacts"),
        approved_plan=approved_plan,
    )

    result = execute_office_work(state | {"_runtime": runtime})
    assert result["status"] == "completed", (
        f"inplace custom-dimension must succeed; got {result!r}"
    )
    assert result["success"] is True

    # The plan is a tool artifact: it sits at the canonical path
    # so the user can re-read it after approval, and it is NOT
    # swept into a bucket.
    assert (src / "custom-organize-plan.md").is_file(), (
        "the planner's custom-organize-plan.md must be left at "
        "the source root so the user can re-read it"
    )
    assert not (src / "unmatched" / "custom-organize-plan.md").exists(), (
        "tool-produced plan must not be classified as "
        "__unmatched__ and copied into an unmatched/ bucket — "
        "this is the task-afc50de4fa71 regression"
    )
    # No stray ``unmatched/`` directory should have been created
    # at all in this scenario.
    assert not (src / "unmatched").exists(), (
        "no unmatched/ bucket should be created when every user "
        "file is matched by the plan"
    )

    # The LLM never saw the tool-produced plan.  The first
    # (and only) execution prompt must not include
    # ``custom-organize-plan.md`` as a file to classify.
    execution_prompt = runtime.prompts[-1]
    assert "custom-organize-plan.md" not in execution_prompt, (
        "the LLM classifier must never see tool-produced plan "
        "files in its input — they are not user content"
    )

    # The final organization-plan.md is also a tool artifact and
    # must sit at the canonical path (not inside a bucket).
    assert (src / "organization-plan.md").is_file(), (
        "the executor's final organization-plan.md must sit at "
        "the canonical source-root path"
    )
    assert not (src / "Alice" / "January" / "organization-plan.md").exists(), (
        "the executor's final organization-plan.md must not be "
        "classified as a user file and dropped into a bucket"
    )


def test_custom_dimension_inplace_records_audit_and_integrity_verdict(
    tmp_path,
):
    """The custom-dimension path must record the same
    ``audit_snapshot`` / ``move_file`` / ``integrity_verify``
    audit trail as the built-in dimension tools.  Without it the
    custom path is invisible to ``operations-plan.json``-based
    forensics, and any future regression would not leave a
    trace.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "messy"
    src.mkdir()
    (src / "0103" / "1.txt").parent.mkdir(parents=True)
    (src / "0103" / "1.txt").write_text("Student: Alice\nJanuary work\n")

    approved_plan = {
        "buckets": ["Alice/January"],
        "sample_mapping": {"0103/1.txt": "Alice/January"},
        "classification_rule": "trivial",
        "rationale": "trivial",
    }
    runtime = _runtime_with_plan_response(plan=approved_plan, mapping={})
    state = _organize_state_inplace(
        source=str(src),
        artifacts_dir=str(tmp_path / "artifacts"),
        approved_plan=approved_plan,
    )

    result = execute_office_work(state | {"_runtime": runtime})
    assert result["status"] == "completed"

    plan_log = tmp_path / "artifacts" / "operations-plan.json"
    assert plan_log.is_file(), (
        "operations-plan.json must be written by the custom "
        "executor (audit log is part of the contract)"
    )
    actions: list[dict] = []
    with open(plan_log, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                actions.append(json.loads(line))

    kinds = [a.get("action") for a in actions]
    assert "audit_snapshot" in kinds, (
        f"custom executor must record an audit_snapshot; got {kinds!r}"
    )
    assert "move_file" in kinds, (
        f"inplace custom executor must record move_file actions; "
        f"got {kinds!r}"
    )
    assert "remove_empty_dir" in kinds, (
        f"inplace custom executor must rmdir the original "
        f"sub-folders after the move; got {kinds!r}"
    )
    assert "integrity_verify" in kinds, (
        f"custom executor must record a post-run integrity "
        f"verdict; got {kinds!r}"
    )
    assert "delete_file" not in kinds, (
        f"organize must never record a delete_file action; "
        f"got {kinds!r}"
    )

    verify_record = next(a for a in actions if a.get("action") == "integrity_verify")
    assert verify_record.get("errors") == [], (
        f"successful custom organize must produce zero integrity "
        f"errors; got {verify_record.get('errors')!r}"
    )


# ---------------------------------------------------------------------------
# Workspace mode — task-298e13f787ac regression
#
# The user reported that in workspace mode the executor still created
# an ``unmatched/`` directory under ``<output_root>`` and dropped
# ``custom-organize-plan.md`` and ``organization-plan.md`` into it.
#
# Root cause: in workspace mode ``source != output_root`` and the
# planner's plan lives in the artifacts dir, not the source folder.
# The execute-phase inventory walk over the source can therefore
# pick up **stale** copies of ``custom-organize-plan.md`` and
# ``organization-plan.md`` left at the source root by a previous
# inplace run.  The LLM classifier puts them under
# ``__unmatched__`` and the executor copies them into
# ``<output_root>/unmatched/`` — a tool-produced artifact shows
# up as a "user file" the LLM had to classify.
#
# Methodology: the inventory's ``exclude_paths`` must include both
# the current run's write targets (``<output_root>/...``) AND the
# same artifact names at the source root (which is the slot
# previous inplace runs may have left dirty).  The integrity check
# uses the same list, so the produced paths in the audit log stay
# self-consistent.  Inplace mode (where ``source == output_root``)
# is unaffected — the dedup pass collapses the duplicate paths to
# one realpath.
# ---------------------------------------------------------------------------


def test_custom_dimension_workspace_skips_stale_tool_artifacts_at_source(
    tmp_path,
):
    """Headline regression for task-298e13f787ac.  A previous
    inplace run may have left ``custom-organize-plan.md`` and
    ``organization-plan.md`` at the source root.  A subsequent
    workspace-mode organize must NOT treat those stale artifacts
    as user content — they must stay out of the inventory, out of
    the LLM's classifier prompt, and out of the bucket layout.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "messy"
    src.mkdir()
    (src / "0103" / "1.txt").parent.mkdir(parents=True)
    (src / "0103" / "1.txt").write_text("Student: Alice\nJanuary work\n")
    (src / "0207" / "1.txt").parent.mkdir(parents=True)
    (src / "0207" / "1.txt").write_text("Student: Bob\nFebruary work\n")
    # Stale tool artifacts at the source root — exactly the
    # shape task-298e13f787ac was running on, because the
    # shared ``tests/data/2026_rw`` fixture had been left
    # dirty by a prior inplace run.
    (src / "custom-organize-plan.md").write_text(
        "# STALE Custom Organize Plan (from a prior inplace run)\n"
    )
    (src / "organization-plan.md").write_text(
        "# STALE Final Organization Plan (from a prior inplace run)\n"
    )

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    approved_plan = {
        "buckets": ["Alice/January", "Bob/February"],
        "sample_mapping": {"0103/1.txt": "Alice/January"},
        "classification_rule": "trivial",
        "rationale": "trivial",
    }
    runtime = _runtime_with_plan_response(
        plan=approved_plan,
        mapping={"0207/1.txt": "Bob/February"},
    )
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        approved_plan=approved_plan,
    )
    # ``_organize_state`` already sets ``output_mode: "workspace"``.

    result = execute_office_work(state | {"_runtime": runtime})
    assert result["status"] == "completed", (
        f"workspace custom-dimension must succeed; got {result!r}"
    )
    assert result["success"] is True

    output_root = artifacts / "organized-output" / "files"

    # The stale tool artifacts at the source root must NOT be
    # swept into the bucket layout.  This is the exact failure
    # shape reported in task-298e13f787ac.
    assert not (output_root / "unmatched").exists(), (
        f"no unmatched/ bucket should be created in workspace "
        f"mode when every user file is matched; contents: "
        f"{sorted(p.name for p in output_root.iterdir())}"
    )
    assert not (output_root / "unmatched" / "custom-organize-plan.md").exists(), (
        "stale custom-organize-plan.md at the source root must "
        "NOT be copied into <output_root>/unmatched/ during a "
        "workspace organize — this is the task-298e13f787ac "
        "regression"
    )
    assert not (output_root / "unmatched" / "organization-plan.md").exists(), (
        "stale organization-plan.md at the source root must NOT "
        "be copied into <output_root>/unmatched/"
    )

    # The LLM never saw the stale tool artifacts in its input.
    # The execution prompt must not list them as files to classify.
    execution_prompt = runtime.prompts[-1]
    assert "custom-organize-plan.md" not in execution_prompt, (
        "the LLM classifier must never see tool-produced plan "
        "files — neither the new run's plan nor stale copies at "
        "the source root"
    )
    assert "organization-plan.md" not in execution_prompt, (
        "the LLM classifier must never see the executor's "
        "final-plan output — including stale copies at the source"
    )

    # The user's source files were copied (workspace mode is
    # read-only on the source) into the bucket layout.
    assert (output_root / "Alice" / "January" / "0103" / "1.txt").is_file()
    assert (output_root / "Bob" / "February" / "0207" / "1.txt").is_file()

    # The executor's own final plan lands at the canonical
    # artifacts path, NOT inside a bucket.  The stale
    # organization-plan.md at the source root does not get
    # moved there — it is a different file.
    final_plan = artifacts / "organization-plan.md"
    assert final_plan.is_file(), (
        "the executor's final organization-plan.md must land "
        "at the canonical artifacts path"
    )


def test_custom_dimension_workspace_does_not_touch_source(tmp_path):
    """Sanity check that the workspace fix does not regress the
    contract that the source is left untouched.  Stale tool
    artifacts at the source root stay where they are (the
    user can re-read them, the same as in inplace mode); they
    are simply excluded from the inventory walk.
    """
    from agents.office.nodes import execute_office_work

    src = tmp_path / "messy"
    src.mkdir()
    (src / "0103" / "1.txt").parent.mkdir(parents=True)
    (src / "0103" / "1.txt").write_text("Student: Alice\nJanuary work\n")
    stale_plan = src / "custom-organize-plan.md"
    stale_final = src / "organization-plan.md"
    stale_plan.write_text("STALE_PLAN_CONTENT")
    stale_final.write_text("STALE_FINAL_CONTENT")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    approved_plan = {
        "buckets": ["Alice/January"],
        "sample_mapping": {"0103/1.txt": "Alice/January"},
        "classification_rule": "trivial",
        "rationale": "trivial",
    }
    runtime = _runtime_with_plan_response(plan=approved_plan, mapping={})
    state = _organize_state(
        source=str(src),
        artifacts_dir=str(artifacts),
        approved_plan=approved_plan,
    )
    result = execute_office_work(state | {"_runtime": runtime})
    assert result["status"] == "completed"

    # Source is read-only in workspace mode.  Even the stale
    # plan files stay byte-for-byte unchanged.
    assert stale_plan.read_text() == "STALE_PLAN_CONTENT", (
        "workspace mode must not mutate the source; the stale "
        "plan file is left alone for the user to clean up"
    )
    assert stale_final.read_text() == "STALE_FINAL_CONTENT"
    # The original user file is still at the source root, too.
    assert (src / "0103" / "1.txt").read_text() == "Student: Alice\nJanuary work\n"
