"""Unit tests for ``framework.major_step``.

Covers:
- Validation of inputs (step_key, summary_facts, required fields).
- Idempotence on ``(step_key, round)``.
- Distinct rows for distinct rounds.
- ``waiting_for_user`` lifecycle leaves ``ended_at`` None.
- Terminal lifecycle states set ``ended_at``.
- Terminal protection: later non-terminal events are appended to events with
  ``ignored_after_terminal=true`` and not to ``major_step_rows``.
- Visual-state default mapping for all 10 lifecycle values.
- Sink fan-out: when ``orchestrator_task_id != task_id`` the supplied sink
  receives the event.
- Registry-based resolution: ``resolve_progress_sink`` returns an HTTP sink
  when registry finds a service URL.
- Concurrent writes from 2 threads produce N events without data loss.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from framework.major_step import (
    LIFECYCLE_CANCELLED,
    LIFECYCLE_CONDITIONAL_PENDING,
    LIFECYCLE_DONE,
    LIFECYCLE_FAILED,
    LIFECYCLE_PENDING,
    LIFECYCLE_RESUMING,
    LIFECYCLE_RUNNING,
    LIFECYCLE_TERMINATED,
    LIFECYCLE_WAITING_FOR_USER,
    LIFECYCLE_WARNING,
    InProcessMajorStepSink,
    MajorStepSink,
    NullMajorStepSink,
    VISUAL_CURRENT,
    VISUAL_DONE,
    VISUAL_FAILED,
    VISUAL_PENDING,
    VISUAL_WARN,
    default_visual_state,
    ensure_major_step_skeleton,
    record_major_step,
    resolve_progress_sink,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def task_store():
    """A small in-memory task store double with the methods ``record_major_step`` uses."""
    store = MagicMock()
    # ``task_store.get_task(task_id).metadata`` -> empty dict by default.
    task = MagicMock()
    task.metadata = {}
    store.get_task.return_value = task
    return store


def _make_task_store(initial_metadata: dict | None = None):
    """Real dict-backed store to exercise ``update_metadata`` semantics."""
    metadata = dict(initial_metadata or {})

    class _Store:
        def __init__(self) -> None:
            self._tasks: dict[str, dict] = {"task-x": {"metadata": metadata}}

        def get_task(self, task_id: str):
            task = self._tasks.get(task_id)
            if task is None:
                return None
            wrapped = MagicMock()
            wrapped.metadata = task["metadata"]
            return wrapped

        def update_metadata(self, task_id: str, delta: dict) -> None:
            if task_id not in self._tasks:
                return
            self._tasks[task_id]["metadata"].update(delta)

    return _Store()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_empty_task_id_raises(self, task_store):
        with pytest.raises(ValueError, match="task_id"):
            record_major_step(
                "",
                step_key="compass.received",
                title="Compass receiving",
                agent="compass",
                task_store=task_store,
            )

    def test_empty_title_raises(self, task_store):
        with pytest.raises(ValueError, match="title"):
            record_major_step(
                "task-1",
                step_key="compass.received",
                title="",
                agent="compass",
                task_store=task_store,
            )

    def test_empty_agent_raises(self, task_store):
        with pytest.raises(ValueError, match="agent"):
            record_major_step(
                "task-1",
                step_key="compass.received",
                title="Compass receiving",
                agent="",
                task_store=task_store,
            )

    def test_uppercase_step_key_rejected(self, task_store):
        with pytest.raises(ValueError, match="step_key"):
            record_major_step(
                "task-1",
                step_key="Compass.Received",
                title="x",
                agent="compass",
                task_store=task_store,
            )

    def test_step_key_with_dash_rejected(self, task_store):
        with pytest.raises(ValueError, match="step_key"):
            record_major_step(
                "task-1",
                step_key="compass.received-now",
                title="x",
                agent="compass",
                task_store=task_store,
            )

    def test_step_key_single_segment_rejected(self, task_store):
        with pytest.raises(ValueError, match="step_key"):
            record_major_step(
                "task-1",
                step_key="received",
                title="x",
                agent="compass",
                task_store=task_store,
            )

    def test_negative_round_rejected(self, task_store):
        with pytest.raises(ValueError, match="round"):
            record_major_step(
                "task-1",
                step_key="compass.received",
                title="x",
                agent="compass",
                round=-1,
                task_store=task_store,
            )

    def test_unknown_lifecycle_state_rejected(self, task_store):
        with pytest.raises(ValueError, match="lifecycle_state"):
            record_major_step(
                "task-1",
                step_key="compass.received",
                title="x",
                agent="compass",
                lifecycle_state="frobnicated",
                task_store=task_store,
            )

    def test_unknown_visual_state_rejected(self, task_store):
        with pytest.raises(ValueError, match="visual_state"):
            record_major_step(
                "task-1",
                step_key="compass.received",
                title="x",
                agent="compass",
                visual_state="shiny",
                task_store=task_store,
            )

    def test_secret_key_in_facts_rejected(self, task_store):
        with pytest.raises(ValueError, match="secret"):
            record_major_step(
                "task-1",
                step_key="compass.received",
                title="x",
                agent="compass",
                summary_facts={"secret_token": "ghp_abcdefghijklmnop1234"},
                task_store=task_store,
            )

    def test_github_token_in_value_rejected(self, task_store):
        with pytest.raises(ValueError, match="sensitive"):
            record_major_step(
                "task-1",
                step_key="compass.received",
                title="x",
                agent="compass",
                summary_facts={"note": "leaked ghp_abcdefghijklmnop1234 here"},
                task_store=task_store,
            )

    def test_non_json_facts_rejected(self, task_store):
        with pytest.raises(ValueError, match="JSON"):
            record_major_step(
                "task-1",
                step_key="compass.received",
                title="x",
                agent="compass",
                summary_facts={"bad": {1, 2, 3}},  # set is not JSON-serializable
                task_store=task_store,
            )


# ---------------------------------------------------------------------------
# Visual state mapping
# ---------------------------------------------------------------------------

class TestVisualMapping:
    @pytest.mark.parametrize(
        ("lifecycle", "expected_visual"),
        [
            (LIFECYCLE_PENDING, VISUAL_PENDING),
            (LIFECYCLE_CONDITIONAL_PENDING, "conditional_pending"),
            (LIFECYCLE_RUNNING, VISUAL_CURRENT),
            (LIFECYCLE_WAITING_FOR_USER, VISUAL_WARN),
            (LIFECYCLE_RESUMING, VISUAL_CURRENT),
            (LIFECYCLE_DONE, VISUAL_DONE),
            (LIFECYCLE_WARNING, VISUAL_WARN),
            (LIFECYCLE_FAILED, VISUAL_FAILED),
            (LIFECYCLE_CANCELLED, VISUAL_FAILED),
            (LIFECYCLE_TERMINATED, VISUAL_FAILED),
        ],
    )
    def test_default_visual(self, lifecycle, expected_visual):
        assert default_visual_state(lifecycle) == expected_visual


# ---------------------------------------------------------------------------
# Idempotence on (step_key, round)
# ---------------------------------------------------------------------------

class TestIdempotence:
    def test_same_step_key_round_updates_same_row(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="wd.implementing",
            title="Web Dev implementing",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_RUNNING,
            task_store=store,
        )
        record_major_step(
            "task-x",
            step_key="wd.implementing",
            title="Web Dev implementing",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_DONE,
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        rows = meta["major_step_rows"]
        assert len(rows) == 1
        sik = "wd.implementing#0"
        assert sik in rows
        assert rows[sik]["lifecycle_state"] == LIFECYCLE_DONE
        # The two events are still in the append-only event log.
        assert len(meta["major_step_events"]) == 2

    def test_different_round_creates_distinct_rows(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="wd.fixing_gaps",
            title="fix gaps",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_RUNNING,
            round=0,
            task_store=store,
        )
        record_major_step(
            "task-x",
            step_key="wd.fixing_gaps",
            title="fix gaps",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_RUNNING,
            round=1,
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        assert len(meta["major_step_rows"]) == 2
        assert "wd.fixing_gaps#0" in meta["major_step_rows"]
        assert "wd.fixing_gaps#1" in meta["major_step_rows"]
        # Skeleton is dedup'd: each step_instance_key appears once.
        skel_keys = [r["step_instance_key"] for r in meta["major_step_skeleton"]]
        assert skel_keys.count("wd.fixing_gaps#0") == 1
        assert skel_keys.count("wd.fixing_gaps#1") == 1


class TestSkeletonSeeding:
    def test_seeding_adds_rows_without_events_or_current_step(self):
        store = _make_task_store()
        added = ensure_major_step_skeleton(
            "task-x",
            entries=[
                {
                    "step_key": "office.reading",
                    "title": "Office reading documents",
                    "agent": "office",
                },
                {
                    "step_key": "office.combining",
                    "title": "Office creating combined summary",
                    "agent": "office",
                    "conditional": True,
                },
            ],
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        assert len(added) == 2
        assert [row["step_instance_key"] for row in meta["major_step_skeleton"]] == [
            "office.reading#0",
            "office.combining#0",
        ]
        assert meta.get("major_step_events") is None
        assert meta.get("progress_steps") is None
        assert meta.get("current_major_step") is None

    def test_seeding_dedups_existing_step_instance_keys(self):
        store = _make_task_store(
            {
                "major_step_skeleton": [
                    {
                        "step_key": "office.reading",
                        "step_instance_key": "office.reading#0",
                        "round": 0,
                        "title": "Office reading documents",
                        "agent": "office",
                        "conditional": False,
                    }
                ]
            }
        )
        added = ensure_major_step_skeleton(
            "task-x",
            entries=[
                {
                    "step_key": "office.reading",
                    "title": "Office reading documents",
                    "agent": "office",
                },
                {
                    "step_key": "office.summarizing",
                    "title": "Office summarizing each document",
                    "agent": "office",
                },
            ],
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        assert [row["step_instance_key"] for row in added] == ["office.summarizing#0"]
        assert [row["step_instance_key"] for row in meta["major_step_skeleton"]] == [
            "office.reading#0",
            "office.summarizing#0",
        ]


# ---------------------------------------------------------------------------
# Lifecycle / ended_at semantics
# ---------------------------------------------------------------------------

class TestEndedAt:
    def test_waiting_for_user_leaves_ended_at_none(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="tl.requesting_user_input",
            title="TL asking",
            agent="team-lead",
            lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
            task_store=store,
        )
        row = store.get_task("task-x").metadata["major_step_rows"]["tl.requesting_user_input#0"]
        assert row["ended_at"] is None
        assert "started_at" in row

    def test_terminal_lifecycle_sets_ended_at(self):
        store = _make_task_store()
        for terminal in (LIFECYCLE_DONE, LIFECYCLE_FAILED, LIFECYCLE_CANCELLED, LIFECYCLE_TERMINATED):
            record_major_step(
                "task-x",
                step_key=f"compass.task_{terminal}",
                title=f"task {terminal}",
                agent="compass",
                lifecycle_state=terminal,
                task_store=store,
            )
        meta = store.get_task("task-x").metadata
        for terminal in (LIFECYCLE_DONE, LIFECYCLE_FAILED, LIFECYCLE_CANCELLED, LIFECYCLE_TERMINATED):
            sik = f"compass.task_{terminal}#0"
            assert meta["major_step_rows"][sik]["ended_at"] is not None

    def test_resume_path_emits_resuming_then_done_on_same_step_key(self):
        # Bug task-03db89946011: the compass resume path emits
        # ``compass.asking_output_mode#0`` three times in sequence —
        # WAITING_FOR_USER (initial pause) → RESUMING (user reply received)
        # → DONE (output location accepted). The RESUMING and DONE events
        # must land on the SAME row so the row has a real ``ended_at`` and
        # the timeline does not display a perpetually-running
        # "Compass resuming after output location was selected" step.
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="compass.asking_output_mode",
            title="Compass asking for output location",
            agent="compass",
            lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
            task_store=store,
        )
        record_major_step(
            "task-x",
            step_key="compass.asking_output_mode",
            title="Compass resuming after output location was selected",
            agent="compass",
            lifecycle_state=LIFECYCLE_RESUMING,
            task_store=store,
        )
        record_major_step(
            "task-x",
            step_key="compass.asking_output_mode",
            title="Compass accepted output location",
            agent="compass",
            lifecycle_state=LIFECYCLE_DONE,
            summary_facts={"output_mode": "workspace"},
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        rows = meta["major_step_rows"]
        sik = "compass.asking_output_mode#0"
        # All three events must collapse into a single row.
        assert len(rows) == 1, rows
        row = rows[sik]
        # The terminal event wins: lifecycle_state, visual_state, ended_at
        # must all reflect DONE. The intermediate RESUMING state must not
        # leave the row stuck in current/running.
        assert row["lifecycle_state"] == LIFECYCLE_DONE
        assert row["visual_state"] == VISUAL_DONE
        assert row["ended_at"] is not None
        # The title from the terminal event is what the UI displays; this
        # is intentional because the final state is the one the user
        # cares about. The first two titles remain in the event log.
        assert row["title"] == "Compass accepted output location"
        events = meta["major_step_events"]
        # All three events are recorded in the audit log.
        title_sequence = [ev["title"] for ev in events if ev["step_instance_key"] == sik]
        assert title_sequence == [
            "Compass asking for output location",
            "Compass resuming after output location was selected",
            "Compass accepted output location",
        ]

    def test_late_resuming_event_cannot_reopen_finished_output_mode_step(self):
        # task-200cbd9fa537 exposed an out-of-order duplicate where a late
        # ``resuming`` event for ``compass.asking_output_mode#0`` arrived
        # after the terminal ``done`` event. The row must stay finished
        # instead of regressing back to the "resuming" title/state.
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="compass.asking_output_mode",
            title="Compass asking for output location",
            agent="compass",
            lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
            task_store=store,
        )
        record_major_step(
            "task-x",
            step_key="compass.asking_output_mode",
            title="Compass accepted output location",
            agent="compass",
            lifecycle_state=LIFECYCLE_DONE,
            summary_facts={"output_mode": "workspace"},
            task_store=store,
        )
        record_major_step(
            "task-x",
            step_key="compass.asking_output_mode",
            title="Compass resuming after output location was selected",
            agent="compass",
            lifecycle_state=LIFECYCLE_RESUMING,
            task_store=store,
        )

        meta = store.get_task("task-x").metadata
        row = meta["major_step_rows"]["compass.asking_output_mode#0"]
        assert row["lifecycle_state"] == LIFECYCLE_DONE
        assert row["visual_state"] == VISUAL_DONE
        assert row["title"] == "Compass accepted output location"
        assert row["summary_facts"]["output_mode"] == "workspace"
        late_events = [
            ev
            for ev in meta["major_step_events"]
            if ev["step_instance_key"] == "compass.asking_output_mode#0"
            and ev["title"] == "Compass resuming after output location was selected"
        ]
        assert len(late_events) == 1
        assert late_events[0]["ignored_after_terminal"] is True


# ---------------------------------------------------------------------------
# Terminal protection
# ---------------------------------------------------------------------------

class TestTerminalProtection:
    def test_late_non_terminal_event_ignored_in_rows(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="compass.task_failed",
            title="task failed",
            agent="compass",
            lifecycle_state=LIFECYCLE_FAILED,
            task_store=store,
        )
        # A late wd.implementing event arrives.
        record_major_step(
            "task-x",
            step_key="wd.implementing",
            title="still running",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_RUNNING,
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        # Only the terminal row exists in major_step_rows.
        assert list(meta["major_step_rows"].keys()) == ["compass.task_failed#0"]
        # The late event is in events with ignored_after_terminal=true.
        late_events = [
            e for e in meta["major_step_events"] if e.get("step_key") == "wd.implementing"
        ]
        assert len(late_events) == 1
        assert late_events[0]["ignored_after_terminal"] is True

    def test_completed_intermediate_row_does_not_block_later_running_step(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="compass.dispatched",
            title="Compass dispatching to Office Agent",
            agent="compass",
            lifecycle_state=LIFECYCLE_DONE,
            task_store=store,
        )
        record_major_step(
            "task-x",
            step_key="office.reading",
            title="Office reading documents",
            agent="office",
            lifecycle_state=LIFECYCLE_RUNNING,
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        assert "office.reading#0" in meta["major_step_rows"]
        office_row = meta["major_step_rows"]["office.reading#0"]
        assert office_row["lifecycle_state"] == LIFECYCLE_RUNNING
        late_events = [
            e for e in meta["major_step_events"] if e.get("step_key") == "office.reading"
        ]
        assert len(late_events) == 1
        assert not late_events[0].get("ignored_after_terminal")


# ---------------------------------------------------------------------------
# Pointer fields
# ---------------------------------------------------------------------------

class TestPointerFields:
    def test_new_running_step_closes_previous_active_row(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="compass.dispatched",
            title="Compass dispatching to Team Lead",
            agent="compass",
            lifecycle_state=LIFECYCLE_RUNNING,
            task_store=store,
        )
        record_major_step(
            "task-x",
            step_key="tl.received",
            title="Team Lead receiving dev task",
            agent="team-lead",
            lifecycle_state=LIFECYCLE_RUNNING,
            task_store=store,
        )

        meta = store.get_task("task-x").metadata
        dispatched = meta["major_step_rows"]["compass.dispatched#0"]
        received = meta["major_step_rows"]["tl.received#0"]
        assert dispatched["lifecycle_state"] == LIFECYCLE_DONE
        assert dispatched["visual_state"] == VISUAL_DONE
        assert dispatched["ended_at"] is not None
        assert received["lifecycle_state"] == LIFECYCLE_RUNNING
        assert meta["active_step_instance_key"] == "tl.received#0"

    def test_active_pointer_set_on_running(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="wd.implementing",
            title="Web Dev implementing",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_RUNNING,
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        assert meta["active_step_instance_key"] == "wd.implementing#0"
        assert meta["last_step_instance_key"] == "wd.implementing#0"
        assert meta["failed_step_instance_key"] == ""
        assert meta["terminal_step_instance_key"] == ""

    def test_active_pointer_set_on_waiting(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="tl.requesting_user_input",
            title="TL asking",
            agent="team-lead",
            lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
            task_store=store,
        )
        assert (
            store.get_task("task-x").metadata["active_step_instance_key"]
            == "tl.requesting_user_input#0"
        )

    def test_failed_pointer_set_on_failed_step(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="wd.building",
            title="build",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_FAILED,
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        assert meta["failed_step_instance_key"] == "wd.building#0"

    def test_terminal_pointer_set_on_compass_terminal(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="compass.task_completed",
            title="task done",
            agent="compass",
            lifecycle_state=LIFECYCLE_DONE,
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        assert meta["terminal_step_instance_key"] == "compass.task_completed#0"
        # Active pointer is cleared because the task closed.
        assert meta["active_step_instance_key"] == ""

    def test_in_flight_cancelled_then_terminal_clears_active(self):
        store = _make_task_store()
        # In-flight step starts running.
        record_major_step(
            "task-x",
            step_key="wd.implementing",
            title="Web Dev implementing",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_RUNNING,
            task_store=store,
        )
        # User cancels; in-flight row closes to cancelled.
        record_major_step(
            "task-x",
            step_key="wd.implementing",
            title="Web Dev implementing",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_CANCELLED,
            task_store=store,
        )
        # Compass appends task_cancelled.
        record_major_step(
            "task-x",
            step_key="compass.task_cancelled",
            title="cancelled by user",
            agent="compass",
            lifecycle_state=LIFECYCLE_CANCELLED,
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        assert meta["terminal_step_instance_key"] == "compass.task_cancelled#0"
        # Active pointer cleared because the in-flight row is now terminal.
        assert meta["active_step_instance_key"] == ""


# ---------------------------------------------------------------------------
# Backward compat fields
# ---------------------------------------------------------------------------

class TestLegacyFields:
    def test_progress_steps_projected_from_events(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="compass.received",
            title="Compass receiving request",
            agent="compass",
            lifecycle_state=LIFECYCLE_RUNNING,
            task_store=store,
        )
        record_major_step(
            "task-x",
            step_key="compass.dispatched",
            title="Compass dispatching",
            agent="compass",
            lifecycle_state=LIFECYCLE_DONE,
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        titles = {entry["text"] for entry in meta["progress_steps"]}
        assert "Compass receiving request" in titles
        assert "Compass dispatching" in titles
        assert meta["current_major_step"] == "Compass dispatching"

    def test_progress_steps_replaces_on_idempotent_update(self):
        store = _make_task_store()
        record_major_step(
            "task-x",
            step_key="wd.implementing",
            title="Web Dev implementing",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_RUNNING,
            task_store=store,
        )
        record_major_step(
            "task-x",
            step_key="wd.implementing",
            title="Web Dev implementing",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_DONE,
            task_store=store,
        )
        meta = store.get_task("task-x").metadata
        # Both events in events log, but progress_steps only has one row per sik.
        implementing_rows = [e for e in meta["progress_steps"] if e["text"] == "Web Dev implementing"]
        assert len(implementing_rows) == 1


# ---------------------------------------------------------------------------
# Progress sink fan-out
# ---------------------------------------------------------------------------

class _CaptureSink(MajorStepSink):
    def __init__(self) -> None:
        self.events: list[dict] = []

    def handle_event(self, event: dict) -> None:
        self.events.append(event)


class TestSinkFanOut:
    def test_sink_called_when_orchestrator_differs_from_task_id(self, task_store):
        sink = _CaptureSink()
        record_major_step(
            "downstream-task",
            step_key="wd.implementing",
            title="Web Dev implementing",
            agent="web-dev",
            lifecycle_state=LIFECYCLE_RUNNING,
            orchestrator_task_id="compass-task",
            progress_sink=sink,
            task_store=task_store,
        )
        assert len(sink.events) == 1
        assert sink.events[0]["step_key"] == "wd.implementing"
        assert sink.events[0]["orchestrator_task_id"] == "compass-task"

    def test_sink_always_called_when_orchestrator_id_set(self, task_store):
        # The sink is always invoked when ``orchestrator_task_id`` is set,
        # even if it equals ``task_id`` — cross-process deployments rely on
        # the HTTP fan-out to push the event to the orchestrator's separate
        # TaskStore. Idempotence is enforced inside ``record_major_step``.
        sink = _CaptureSink()
        record_major_step(
            "task-x",
            step_key="compass.received",
            title="x",
            agent="compass",
            lifecycle_state=LIFECYCLE_RUNNING,
            orchestrator_task_id="task-x",  # same as task_id
            progress_sink=sink,
            task_store=task_store,
        )
        assert len(sink.events) == 1
        assert sink.events[0]["step_key"] == "compass.received"


# ---------------------------------------------------------------------------
# Sink resolution
# ---------------------------------------------------------------------------

class TestResolveProgressSink:
    def test_resolve_returns_inprocess_when_task_store_provided(self):
        from framework.major_step import InProcessMajorStepSink as ClassInProcess

        sink = resolve_progress_sink("compass-task", task_store=MagicMock())
        assert isinstance(sink, ClassInProcess)

    def test_resolve_uses_env_var_override(self, monkeypatch):
        from framework.major_step import HttpMajorStepSink as ClassHttp

        monkeypatch.setenv("MAJOR_STEP_SINK_URL", "http://override:9999/events")
        sink = resolve_progress_sink("compass-task", task_store=None)
        assert isinstance(sink, ClassHttp)
        assert sink._callback_url == "http://override:9999/events"

    def test_resolve_uses_registry_when_env_unset(self, monkeypatch):
        from framework.major_step import HttpMajorStepSink as ClassHttp

        monkeypatch.delenv("MAJOR_STEP_SINK_URL", raising=False)
        # Mock the registry client to return a service URL.
        fake_registry = MagicMock()
        fake_registry.discover.return_value = "http://compass:8000"
        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: fake_registry),
        )
        sink = resolve_progress_sink("compass-task", task_store=None)
        assert isinstance(sink, ClassHttp)
        assert sink._callback_url == "http://compass:8000/_major_step/events"

    def test_resolve_returns_null_when_nothing_available(self, monkeypatch):
        monkeypatch.delenv("MAJOR_STEP_SINK_URL", raising=False)
        fake_registry = MagicMock()
        fake_registry.discover.return_value = ""
        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: fake_registry),
        )
        sink = resolve_progress_sink("compass-task", task_store=None)
        assert isinstance(sink, NullMajorStepSink)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_writes_produce_all_events_and_rows(self):
        store = _make_task_store()
        iterations = 50
        errors: list[BaseException] = []

        def writer(thread_id: int) -> None:
            try:
                for i in range(iterations):
                    record_major_step(
                        "task-x",
                        step_key=f"wd.implementing",
                        title=f"Web Dev implementing (t{thread_id} i{i})",
                        agent="web-dev",
                        lifecycle_state=LIFECYCLE_RUNNING,
                        round=(thread_id * 1000) + i,
                        task_store=store,
                    )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=writer, args=(0,))
        t2 = threading.Thread(target=writer, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"writer threads failed: {errors}"
        meta = store.get_task("task-x").metadata
        # 2 threads * 50 rounds = 100 distinct step_instance_keys.
        assert len(meta["major_step_rows"]) == 100
        assert len(meta["major_step_events"]) == 100
        # Skeleton dedup'd: 100 entries.
        assert len(meta["major_step_skeleton"]) == 100
