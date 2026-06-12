"""Tests for the output-mode gate on the office resume path.

The user reported task-115d9fb72c78: the request "please organize folder
in /.../unsorted_rw" went to the dimension round, the user replied
with a dimension ("by type"), and compass immediately dispatched in
workspace mode without ever asking for the output mode.  The init
path has an ``if not office_request.get("output_mode")`` gate that
asks the question, but the resume path's dimension round silently
defaulted to ``"workspace"`` and skipped the gate.

The fix: make the resume dispatcher consult the same gate.  When
the resume reply validates the dimension but the output_mode is
still empty, the task must transition back to ``INPUT_REQUIRED`` and
ask the output-mode question.  When the resume reply validates BOTH
the dimension and the output mode (combined or in one reply), the
task dispatches immediately.  Symmetric: when the user is in the
output-mode round and their reply contains a dimension, the task
must also surface the dimension gate (or dispatch if both resolve).

Methodology-level: the resume dispatcher mirrors the init path's
two-gate logic, so a dimension round never silently drops the user
into the workspace default.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

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


@pytest.fixture()
def compass_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    from agents.compass.agent import CompassAgent, compass_definition

    task_store = InMemoryTaskStore()
    services = _agent_services(task_store=task_store)
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    # No default patch on _complete_office_task — each test decides
    # whether to call ``_patch_complete`` (synchronous shim that
    # records the dispatched office_request) or leave the no-op stub
    # in place.  The bug-reproduction tests (which must NOT dispatch)
    # skip the patch so any background-thread dispatch would be a
    # test failure.
    return agent, task_store


def _send_message(agent, text, *, metadata=None):
    msg = {
        "message": {
            "parts": [{"text": text}],
            "metadata": metadata or {},
        }
    }
    return asyncio.run(agent.handle_message(msg))


def _patch_dispatch(monkeypatch, captured: dict, task_store=None):
    """Replace the real office launcher with a capture shim so we can
    see what office_request the dispatcher tried to send."""

    def _fake_dispatch_office_request(task_id, user_text, office_request, registry, log):
        captured["office_request"] = dict(office_request)
        captured["task_id"] = task_id
        return {"status": "completed", "state": "TASK_STATE_COMPLETED"}

    monkeypatch.setattr(
        "agents.compass.agent._dispatch_office_request",
        _fake_dispatch_office_request,
    )


def _patch_complete(monkeypatch, captured: dict):
    """Make the background-thread ``_complete_office_task`` run
    synchronously in the test thread, so we can read ``captured`` from
    the test before it returns.  The shim looks up
    ``_dispatch_office_request`` through ``agents.compass.agent`` (not
    via a top-of-file import) so it sees the shimmed version when
    ``_patch_dispatch`` is also installed in the same test.
    """

    def _sync_complete(self, *, task_id, user_text, office_request):
        from framework.devlog import AgentLogger
        from framework.tools.registry import get_registry
        import agents.compass.agent as compass_module

        registry = get_registry()
        log = AgentLogger(task_id=task_id, agent_name=self.definition.agent_id)
        try:
            dispatch_data = compass_module._dispatch_office_request(
                task_id, user_text, office_request, registry, log,
            )
        except Exception as exc:
            dispatch_data = {"status": "error", "message": str(exc)}
        self._finalize_office_task_result(
            task_id=task_id,
            office_request=office_request,
            dispatch_data=dispatch_data,
        )

    monkeypatch.setattr(
        "agents.compass.agent.CompassAgent._complete_office_task",
        _sync_complete,
    )


# At import time we need the real ``_dispatch_office_request`` symbol
# for the shim above; importing the module at the top of the file
# pulls it in.  We re-import here to keep the helper readable.
from agents.compass.agent import _dispatch_office_request  # noqa: E402


# ---------------------------------------------------------------------------
# The reported bug: dimension-only reply must trigger the output-mode round
# ---------------------------------------------------------------------------


def test_organize_dimension_only_reply_pauses_for_output_mode(
    compass_agent, monkeypatch
):
    """The reported bug from task-115d9fb72c78.

    Sequence:
      1. User: 'please organize folder in /.../unsorted_rw'
      2. Compass asks for the dimension (input_required).
      3. User replies with a dimension only: 'by type'
      4. Compass MUST ask for the output mode — not dispatch in workspace.
    """
    captured: dict = {}
    _patch_dispatch(monkeypatch, captured)

    agent, task_store = compass_agent

    # Step 1+2: initial message → input_required on the dimension round.
    init_result = _send_message(
        agent,
        "please organize folder in /Users/aibot/projects/constellation/tests/data/unsorted_rw",
    )
    init_task_id = init_result["task_id"]
    init_ui = init_result.get("ui_update") or {}
    assert init_ui.get("task_status") == "TASK_STATE_INPUT_REQUIRED", (
        f"first round must pause for dimension; got: {init_ui}"
    )

    # Step 3+4: user replies with a dimension only.  Compass must
    # pause for the output mode (NOT dispatch in workspace).
    resume_result = asyncio.run(agent.resume_task(init_task_id, "by type"))
    resume_ui = resume_result.get("ui_update") or {}
    assert resume_ui.get("task_status") == "TASK_STATE_INPUT_REQUIRED", (
        f"replying with a dimension alone must pause for output mode; "
        f"got: {resume_ui}"
    )
    # Office was NOT dispatched.
    assert "office_request" not in captured, (
        f"office must NOT be dispatched when output mode is missing; "
        f"captured: {captured!r}"
    )


def test_organize_combined_reply_dispatches_with_inplace(
    compass_agent, monkeypatch
):
    """Same flow as the bug, but the user types both the dimension and
    the output mode in the dimension round.  Compass must dispatch
    immediately in inplace mode.

    This is the cross-aware positive case: it proves the gate
    fires when output_mode is empty AND skips when it is set.
    """
    captured: dict = {}
    _patch_complete(monkeypatch, captured)
    _patch_dispatch(monkeypatch, captured)

    agent, task_store = compass_agent

    init_result = _send_message(
        agent,
        "please organize folder in /Users/aibot/projects/constellation/tests/data/unsorted_rw",
    )
    init_task_id = init_result["task_id"]

    resume_result = asyncio.run(agent.resume_task(init_task_id, "by type in place"))
    assert "office_request" in captured, (
        f"combined dimension+mode reply must dispatch; got: {resume_result!r}"
    )
    req = captured["office_request"]
    assert req["output_mode"] == "inplace"
    assert req["organize_dimension"] == "type"
    assert (req.get("organize_metadata") or {}).get("organizeGroupBy") == "type"


def test_organize_dimension_workspace_reply_dispatches_with_workspace(
    compass_agent, monkeypatch
):
    """Symmetric positive case: user types 'by type workspace' in the
    dimension round.  Compass must dispatch in workspace mode.
    """
    captured: dict = {}
    _patch_complete(monkeypatch, captured)
    _patch_dispatch(monkeypatch, captured)

    agent, _task_store = compass_agent

    init_result = _send_message(
        agent,
        "please organize folder in /Users/aibot/projects/constellation/tests/data/unsorted_rw",
    )
    init_task_id = init_result["task_id"]

    resume_result = asyncio.run(agent.resume_task(init_task_id, "by type workspace"))
    assert "office_request" in captured, (
        f"combined reply must dispatch; got: {resume_result!r}"
    )
    req = captured["office_request"]
    assert req["output_mode"] == "workspace"
    assert req["organize_dimension"] == "type"


# ---------------------------------------------------------------------------
# Sanity: when the user types BOTH intents in the output-mode round
# (reverse order), the dimension gate is symmetric.
# ---------------------------------------------------------------------------


def test_organize_output_mode_round_dimension_only_pauses_for_dimension(
    compass_agent, monkeypatch
):
    """The reverse: compass asks output mode first, the user replies
    with a dimension only.  The output-mode round is cross-aware, so
    the dimension must be saved, but compass must also ask for the
    output mode (not dispatch in workspace by default).
    """
    captured: dict = {}
    _patch_dispatch(monkeypatch, captured)

    agent, _task_store = compass_agent

    # Use a request that already includes a dimension AND a known
    # source path, so the init path lands in the output-mode round.
    init_result = _send_message(
        agent,
        "please organize folder in /Users/aibot/projects/constellation/tests/data/unsorted_rw by type",
    )
    init_task_id = init_result["task_id"]
    init_ui = init_result.get("ui_update") or {}
    assert init_ui.get("task_status") == "TASK_STATE_INPUT_REQUIRED", (
        f"first round must pause for output mode when dimension is given; "
        f"got: {init_ui}"
    )

    # User replies with a dimension phrase; the output-mode contract
    # will reject it, so cross-aware should save the dimension and
    # re-ask for the output mode.
    resume_result = asyncio.run(agent.resume_task(init_task_id, "by size"))
    resume_ui = resume_result.get("ui_update") or {}
    assert resume_ui.get("task_status") == "TASK_STATE_INPUT_REQUIRED", (
        f"dimension-only reply in output-mode round must re-ask for mode; "
        f"got: {resume_ui}"
    )
    # Office was NOT dispatched.
    assert "office_request" not in captured, (
        f"office must NOT dispatch when output mode is missing; "
        f"captured: {captured!r}"
    )
