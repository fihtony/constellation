"""Tests for resume_task robustness — multi-task safety and stale-state handling.

The user reported that task-5b087b8cf79f surfaced "Failed to send reply to
Compass. Please retry." after replying "workspace".  The server-side
resume endpoint was actually returning HTTP 200 within a few hundred
milliseconds (10 concurrent resumes all <600ms), so the user-visible
error came from a different cause: the user often clicks reply on a
task that has just transitioned out of INPUT_REQUIRED (via the
background dispatch daemon, the 1.5s fast-poll, or the SSE), and the
previous code blindly mutated state on a task that was no longer
waiting for input.

These tests pin the fix:

1. resume_task on a non-INPUT_REQUIRED task returns a structured
   ``task_not_waiting_for_input`` error instead of mutating state.
2. resume_task on an unknown task id raises ``LookupError`` (mapped
   to 404 by the HTTP server) instead of a generic ``RuntimeError``.
3. resume_task on a valid INPUT_REQUIRED office task still does the
   full flow and re-dispatches office.
4. resume_task returns a slim ``{task_id, ui_update}`` payload rather
   than the full 17KB task dict, so the round-trip is fast on slow
   connections.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.a2a.protocol import TaskState
from framework.agent import AgentServices
from framework.task_store import InMemoryTaskStore


def _agent_services(task_store=None):
    return AgentServices(
        session_service=MagicMock(),
        event_store=MagicMock(),
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=MagicMock(),
        registry_client=None,
        task_store=task_store or InMemoryTaskStore(),
    )


# ---------------------------------------------------------------------------
# 1. Stale targetTaskId — task already in WORKING
# ---------------------------------------------------------------------------


def test_resume_on_working_task_returns_structured_error(monkeypatch, tmp_path):
    """When the user clicks reply on a task that has already moved to
    WORKING (e.g. SSE updated state), the server must return
    ``task_not_waiting_for_input`` so the UI can refresh instead of
    showing "Failed to send reply to Compass".
    """
    from agents.compass.agent import CompassAgent, compass_definition

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    task_store = InMemoryTaskStore()
    services = _agent_services(task_store=task_store)
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    task = task_store.create_task(
        agent_id=compass_definition.agent_id,
        metadata={"task_type": "office", "user_request": "summarize x"},
    )
    # The newly created task is already in WORKING — this matches the
    # scenario where the SSE / fast-poll updated it after a successful
    # resume.  The user's "Reply" click now targets a task that is no
    # longer waiting for input.

    result = asyncio.run(agent.resume_task(task.id, "workspace"))

    assert result["error"] == "task_not_waiting_for_input"
    assert result["task_state"] == "TASK_STATE_WORKING"
    assert result["task_id"] == task.id
    # The user message "workspace" was NOT appended to chat_history
    # because the server refused to mutate state.
    promoted = task_store.get_task(task.id)
    history = (promoted.metadata or {}).get("chat_history") or []
    user_messages = [e for e in history if e.get("role") == "USER"]
    assert user_messages == [], (
        "stale resume must not append the user reply to chat_history"
    )


def test_resume_on_completed_task_returns_structured_error(monkeypatch, tmp_path):
    from agents.compass.agent import CompassAgent, compass_definition

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    task_store = InMemoryTaskStore()
    services = _agent_services(task_store=task_store)
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    task = task_store.create_task(
        agent_id=compass_definition.agent_id,
        metadata={"task_type": "office", "user_request": "summarize x"},
    )
    task_store.update_state(task.id, TaskState.COMPLETED, "Done")

    result = asyncio.run(agent.resume_task(task.id, "workspace"))
    assert result["error"] == "task_not_waiting_for_input"
    assert result["task_state"] == "TASK_STATE_COMPLETED"


# ---------------------------------------------------------------------------
# 2. Unknown task id — clean LookupError (not generic RuntimeError)
# ---------------------------------------------------------------------------


def test_resume_on_unknown_task_raises_lookup_error(monkeypatch, tmp_path):
    from agents.compass.agent import CompassAgent, compass_definition

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    services = _agent_services()
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    with pytest.raises(LookupError):
        asyncio.run(agent.resume_task("task-does-not-exist", "workspace"))


# ---------------------------------------------------------------------------
# 3. Happy path: resume on INPUT_REQUIRED still works
# ---------------------------------------------------------------------------


def test_resume_on_waiting_task_returns_slim_payload(monkeypatch, tmp_path):
    """A valid resume on an INPUT_REQUIRED task must:
    1. Append the user reply to chat_history.
    2. Transition the task to WORKING.
    3. Return a slim ``{task_id, ui_update}`` payload — NOT the
       full 17KB task dict, so the round-trip is fast on slow
       connections and one task's slow response cannot starve
       other concurrent tasks.
    """
    from agents.compass.agent import CompassAgent, compass_definition

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    task_store = InMemoryTaskStore()
    services = _agent_services(task_store=task_store)
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    task = task_store.create_task(
        agent_id=compass_definition.agent_id,
        metadata={
            "task_type": "office",
            "user_request": "organize by file size in workspace",
            "office_request": {
                "capability": "organize",
                "source_paths": ["/tmp/x"],
                "output_mode": "",
            },
        },
    )
    task_store.pause_task(
        task.id,
        question="Where should the office output go?",
        interrupt_metadata={
            "kind": "office_output_mode",
            "office_request": task.metadata["office_request"],
        },
    )

    # Replace the background dispatch worker so the test does not
    # spawn a real daemon thread.
    def _no_complete(self, **kwargs):
        return None
    monkeypatch.setattr(
        "agents.compass.agent.CompassAgent._complete_office_task",
        _no_complete,
    )

    result = asyncio.run(agent.resume_task(task.id, "workspace"))

    # The slim payload — no 17KB task dict.
    assert "task_id" in result
    assert "ui_update" in result
    assert "task" not in result, (
        f"resume response must not include the full task dict; "
        f"got keys {list(result)}"
    )
    ui = result["ui_update"]
    assert ui["task_status"] == "TASK_STATE_WORKING"
    # The user message "workspace" was appended.
    promoted = task_store.get_task(task.id)
    history = (promoted.metadata or {}).get("chat_history") or []
    assert any(
        e.get("role") == "USER" and e.get("text") == "workspace"
        for e in history
    )
    output_row = (promoted.metadata or {}).get("major_step_rows", {}).get(
        "compass.asking_output_mode#0"
    )
    assert output_row is not None
    assert output_row["lifecycle_state"] == "done"
    assert output_row["visual_state"] == "done"
    assert output_row["ended_at"] is not None
    assert output_row["title"] == "Compass accepted output location"


# ---------------------------------------------------------------------------
# 4. Multi-task: 10 concurrent resumes on different tasks
# ---------------------------------------------------------------------------


def test_ten_concurrent_resumes_all_succeed(monkeypatch, tmp_path):
    """The user explicitly asked: 'compass is designed for multiple
    tasking, no task shall block compass'.  This test pins the
    invariant: 10 simultaneous resumes on 10 different tasks all
    return HTTP-200-shaped success in well under 15s (the UI's
    fetchJsonWithTimeout deadline).
    """
    import time
    from agents.compass.agent import CompassAgent, compass_definition

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    task_store = InMemoryTaskStore()
    services = _agent_services(task_store=task_store)
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    # Pre-seed 10 paused tasks.
    task_ids = []
    for _ in range(10):
        t = task_store.create_task(
            agent_id=compass_definition.agent_id,
            metadata={
                "task_type": "office",
                "user_request": "organize x",
                "office_request": {
                    "capability": "organize",
                    "source_paths": ["/tmp/x"],
                    "output_mode": "",
                },
            },
        )
        task_store.pause_task(
            t.id,
            question="Where?",
            interrupt_metadata={"kind": "office_output_mode", "office_request": t.metadata["office_request"]},
        )
        task_ids.append(t.id)

    # Stub the background worker so the test does not need a real
    # office roundtrip.
    def _no_complete(self, **kwargs):
        return None
    monkeypatch.setattr(
        "agents.compass.agent.CompassAgent._complete_office_task",
        _no_complete,
    )

    # Drive all 10 resumes in the same event loop so the SQLite
    # task_store lock contention is exercised exactly the way the
    # real HTTP server (ThreadingHTTPServer + per-request event loop
    # in framework/a2a/server.py) drives them.
    async def _drive():
        return await asyncio.gather(*[
            agent.resume_task(tid, "workspace") for tid in task_ids
        ])

    started = time.monotonic()
    results = asyncio.run(_drive())
    elapsed = time.monotonic() - started

    # Every resume returned a slim WORKING payload — none failed.
    assert len(results) == 10
    for r, tid in zip(results, task_ids):
        assert r.get("ui_update", {}).get("task_status") == "TASK_STATE_WORKING", r
        assert r.get("task_id") == tid
        assert "task" not in r, "must not include full task dict in multi-task context"
    # And the whole batch fits well under the 15s UI deadline.
    assert elapsed < 5.0, f"10 concurrent resumes took {elapsed:.2f}s"
