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
