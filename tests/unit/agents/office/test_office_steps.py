"""Tests for the plan-output gate major-step emitters.

This file covers Task 7 of the office plan-output gate plan: the three
new emitter helpers in ``agents.office.office_steps``:

- ``emit_validating_plan_output``  (non-conditional)
- ``emit_reconciling_plan_output`` (conditional, per-round)
- ``emit_gate_exhausted``          (conditional, warning row)
"""
from __future__ import annotations

from agents.office import office_steps


class _Sink:
    def __init__(self):
        self.events = []

    def handle_event(self, event):
        self.events.append(event)


def _state():
    return {
        "capability": "summarize",
        "_compass_task_id": "task-1",
        "_task_store": None,
        "_major_step_progress_sink": None,
    }


def test_emit_validating_plan_output_running():
    sink = _Sink()
    state = {**_state(), "_major_step_progress_sink": sink}
    office_steps.emit_validating_plan_output(
        state,
        lifecycle_state="running",
        summary_template="validating {planned_count}",
        summary_facts={"planned_count": 3},
    )
    assert sink.events[0]["step_key"] == "office.validating_plan_output"
    assert sink.events[0]["lifecycle_state"] == "running"


def test_emit_reconciling_plan_output_emits_round():
    sink = _Sink()
    state = {**_state(), "_major_step_progress_sink": sink}
    office_steps.emit_reconciling_plan_output(
        state,
        lifecycle_state="running",
        round=2,
        summary_template="reconciling round {round}",
        summary_facts={"round": 2},
    )
    assert sink.events[0]["step_key"] == "office.reconciling_plan_output"
    assert sink.events[0]["step_instance_key"] == "office.reconciling_plan_output#2"
    assert sink.events[0]["round"] == 2


def test_emit_gate_exhausted_warning():
    sink = _Sink()
    state = {**_state(), "_major_step_progress_sink": sink}
    office_steps.emit_gate_exhausted(
        state,
        summary_facts={"round_count": 3},
    )
    assert sink.events[0]["step_key"] == "office.gate_exhausted"
    assert sink.events[0]["lifecycle_state"] == "warning"
