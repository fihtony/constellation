"""Unit tests for the office-organize dimension clarification round-trip.

These tests cover the path that turns a ``needs_clarification`` payload
into an interactive user question rather than a failed task.  The path
crosses three layers:

1. ``agents.office.nodes.analyze_request`` returns the
   ``needs_clarification`` payload (already covered by
   ``test_office_organize_dimensions.py``).
2. ``agents.office.agent.OfficeAgent.handle_message._run`` detects the
   payload and promotes the office task to ``TASK_STATE_INPUT_REQUIRED``
   with the structured metadata attached.
3. ``agents.compass.agent.OfficeAgent.CompassAgent.handle_message``
   detects the same state on the office dispatch result and pauses its
   own task with a user-facing question.  When the user replies, the
   ``resume_task`` branch validates the reply and forwards it to the
   same waiting office agent session.

The compass helpers under test (the new ones) are:

- :func:`_normalize_organize_dimension`
- :func:`_office_dispatch_awaiting_input`
- :func:`_office_interrupt_kind`
- :func:`_resolve_office_resume_reply`
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.agent import AgentServices
from framework.a2a.protocol import TaskState
from framework.checkpoint import InMemoryCheckpointer
from framework.task_store import InMemoryTaskStore


# ---------------------------------------------------------------------------
# Stubs / fixtures
# ---------------------------------------------------------------------------


def _agent_services(task_store=None, runtime=None):
    return AgentServices(
        session_service=MagicMock(),
        event_store=MagicMock(),
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=runtime or MagicMock(),
        registry_client=None,
        task_store=task_store or InMemoryTaskStore(),
    )


def _make_execution_contract():
    """Build a minimal but valid execution contract for the office agent."""
    from framework.execution_contract import ExecutionContract

    contract = ExecutionContract(
        profile_name="office",
        allowed_tools=[
            "read_txt", "read_pdf", "read_docx", "read_csv",
            "read_xlsx", "read_xls", "read_pptx", "list_directory",
            "write_workspace", "write_file", "organize_folder",
            "organize_move_file",
        ],
        workflow_ref="config/workflows/office_task.yaml",
        workspace_root="/tmp",
    )
    contract.checksum = contract.compute_checksum()
    return contract


# ---------------------------------------------------------------------------
# Office side: needs_clarification → pause_task
# ---------------------------------------------------------------------------


def test_office_workflow_result_carries_needs_clarification_payload(monkeypatch, tmp_path):
    """The office workflow must propagate the analyze_request payload to the
    background ``_run`` so it can call ``pause_task`` instead of
    ``fail_task``.
    """
    monkeypatch.setenv("OFFICE_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(tmp_path / "ws"))

    from agents.office.nodes import analyze_request

    state = {
        "source_paths": ["/tmp/some/folder"],
        "output_mode": "workspace",
        "capability": "organize",
        "user_request": "please organize this folder",
        "_message_metadata": {},
        "_compass_task_id": "test-task",
    }
    out = analyze_request(state)
    assert out["error"] == "missing_organize_dimension"
    assert "needs_clarification" in out
    payload = out["needs_clarification"]
    assert payload["missing"] == "organizeGroupBy"
    assert {opt["id"] for opt in payload["options"]} == {
        "size", "type", "created_time", "modified_time",
        "accessed_time", "filename",
    }
    assert "user_message" in payload


def test_office_handle_message_pauses_task_on_clarification(monkeypatch, tmp_path):
    """When the office workflow returns a ``needs_clarification`` payload,
    ``OfficeAgent.handle_message`` must promote the task to
    ``TASK_STATE_INPUT_REQUIRED`` and attach the structured interrupt
    metadata so the orchestrator can read it back.
    """
    monkeypatch.setenv("OFFICE_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    from agents.office.agent import OfficeAgent, office_definition
    from agents.office.nodes import analyze_request

    # Drive the workflow to return a needs_clarification payload by stubbing
    # the compiled workflow's invoke.
    needs_clarification_payload = {
        "missing": "organizeGroupBy",
        "options": [
            {"id": "size", "label": "size"},
            {"id": "type", "label": "type"},
        ],
        "user_message": "Office organize needs a grouping dimension.",
    }
    fake_result = {
        "summary": "missing_organize_dimension",
        "success": False,
        "error": "missing_organize_dimension",
        "capability": "organize",
        "needs_clarification": needs_clarification_payload,
    }

    task_store = InMemoryTaskStore()
    agent = OfficeAgent(definition=office_definition, services=_agent_services(task_store=task_store))

    # Compile the workflow so the daemon thread can invoke it.  We
    # immediately swap ``invoke`` for a coroutine that returns the fake
    # result, so the workflow's real I/O is never executed.
    asyncio.run(agent.start())
    assert agent._compiled_workflow is not None, "start() should compile the workflow"

    async def _fake_invoke(state, config):
        return fake_result

    agent._compiled_workflow.invoke = _fake_invoke  # type: ignore[assignment]

    payload = {
        "task_description": "please organize this folder",
        "source_paths": ["/tmp/some/folder"],
        "capability": "organize",
        "output_mode": "workspace",
        "orchestrator_task_id": "compass-clarify-test",
        "callback_url": "",
        "executionContract": _make_execution_contract().to_dict()
            if hasattr(_make_execution_contract(), "to_dict")
            else _make_execution_contract().__dict__,
    }

    result = asyncio.run(agent.handle_message({
        "message": {
            "parts": [{"text": "please organize this folder"}],
            "metadata": payload,
        }
    }))

    # The HTTP-shape response is the task dict at submission time; the
    # background worker is the one that promotes the state.  Wait briefly
    # for the daemon thread to finish.
    import time
    for _ in range(50):
        if task_store.get_task(result["task"]["id"]).status.state == TaskState.INPUT_REQUIRED:
            break
        time.sleep(0.02)

    promoted = task_store.get_task(result["task"]["id"])
    assert promoted.status.state == TaskState.INPUT_REQUIRED, (
        f"expected INPUT_REQUIRED, got {promoted.status.state}"
    )
    interrupt = (promoted.metadata or {}).get("_interrupt") or {}
    assert interrupt.get("kind") == "office_clarification"
    assert interrupt.get("needs_clarification") == needs_clarification_payload
    # The user_message must surface as the question text.
    message = promoted.status.message
    assert message is not None
    assert "Office organize needs a grouping dimension." in message.text()


def test_office_resume_task_reuses_same_task_with_updated_metadata(monkeypatch, tmp_path):
    """OfficeAgent.resume_task should continue the existing office task.

    The task id must stay the same and the rebuilt workflow state must
    carry the resolved organizeGroupBy metadata instead of creating a
    brand new office task / container cycle.
    """
    monkeypatch.setenv("OFFICE_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    from agents.office.agent import OfficeAgent, office_definition

    task_store = InMemoryTaskStore()
    agent = OfficeAgent(
        definition=office_definition,
        services=_agent_services(task_store=task_store),
    )
    asyncio.run(agent.start())
    assert agent._compiled_workflow is not None

    captured: dict[str, object] = {}

    async def _fake_invoke(state, config):
        captured["task_id"] = state.get("_task_id")
        captured["organize_dimension"] = state.get("organize_dimension")
        return {
            "summary": "organized by size",
            "success": True,
            "capability": "organize",
            "output_mode": "workspace",
        }

    agent._compiled_workflow.invoke = _fake_invoke  # type: ignore[assignment]

    request_metadata = {
        "source_paths": ["/tmp/some/folder"],
        "capability": "organize",
        "output_mode": "workspace",
        "executionContract": _make_execution_contract().to_dict(),
    }
    task = task_store.create_task(
        agent_id=office_definition.agent_id,
        task_id="office-task-clarify-1",
        metadata={
            "user_text": "please organize this folder",
            "source_paths": ["/tmp/some/folder"],
            "capability": "organize",
            "output_mode": "workspace",
            "callback_url": "",
            "request_metadata": request_metadata,
        },
    )
    task_store.pause_task(
        task.id,
        question="Office organize needs a grouping dimension.",
        interrupt_metadata={
            "kind": "office_clarification",
            "needs_clarification": {
                "missing": "organizeGroupBy",
                "options": [
                    {"id": "size", "label": "size"},
                    {"id": "type", "label": "type"},
                ],
                "user_message": "Office organize needs a grouping dimension.",
            },
        },
    )

    result = asyncio.run(agent.resume_task(task.id, "size"))

    assert result["task"]["id"] == task.id
    assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert captured["task_id"] == task.id
    assert captured["organize_dimension"] == "size"
    persisted = task_store.get_task(task.id)
    assert persisted is not None
    assert persisted.metadata["request_metadata"]["organizeGroupBy"] == "size"


# ---------------------------------------------------------------------------
# Compass helpers
# ---------------------------------------------------------------------------


def test_compass_normalize_organize_dimension_accepts_canonical_ids():
    from agents.compass.agent import _normalize_organize_dimension

    for dim in ("size", "type", "created_time", "modified_time", "accessed_time", "filename"):
        assert _normalize_organize_dimension(dim) == dim
        assert _normalize_organize_dimension(dim.upper()) == dim
        assert _normalize_organize_dimension(f"  {dim}  ") == dim


def test_compass_normalize_organize_dimension_accepts_keywords():
    from agents.compass.agent import _normalize_organize_dimension

    assert _normalize_organize_dimension("大小") == "size"
    assert _normalize_organize_dimension("按修改时间") == "modified_time"
    assert _normalize_organize_dimension("file size") == "size"
    assert _normalize_organize_dimension("by name") == "filename"


def test_compass_normalize_organize_dimension_rejects_garbage():
    from agents.compass.agent import _normalize_organize_dimension

    assert _normalize_organize_dimension("") == ""
    assert _normalize_organize_dimension("   ") == ""
    assert _normalize_organize_dimension("students") == ""
    assert _normalize_organize_dimension("按颜色") == ""
    assert _normalize_organize_dimension("size extra") == "size"


def test_compass_office_dispatch_awaiting_input_helper():
    from agents.compass.agent import _office_dispatch_awaiting_input

    # Positive cases
    assert _office_dispatch_awaiting_input({
        "status": "input-required",
        "state": "TASK_STATE_INPUT_REQUIRED",
        "needs_clarification": {"missing": "organizeGroupBy"},
    })
    # Missing payload is not enough — we still need a real clarification.
    assert not _office_dispatch_awaiting_input({
        "status": "input-required",
        "state": "TASK_STATE_INPUT_REQUIRED",
    })
    # Non-clarification states.
    assert not _office_dispatch_awaiting_input({
        "status": "completed",
        "state": "TASK_STATE_COMPLETED",
    })
    assert not _office_dispatch_awaiting_input({
        "status": "error",
        "state": "TASK_STATE_FAILED",
    })


def test_compass_office_task_to_dispatch_data_maps_resume_input_required_response():
    from agents.compass.agent import _office_task_to_dispatch_data

    reply_contract = {
        "schema_version": 1,
        "kind": "approve_or_modify",
        "reask_message": "Please reply with `approve` or `modify: <change>`.",
    }
    office_response = {
        "task": {
            "id": "office-task-1",
            "status": {
                "state": "TASK_STATE_INPUT_REQUIRED",
                "message": {
                    "parts": [{"text": "Review the drafted plan and reply approve."}],
                },
            },
            "artifacts": [
                {
                    "parts": [{"text": "Office drafted an organize plan."}],
                    "metadata": {},
                }
            ],
            "metadata": {
                "_interrupt": {
                    "needs_clarification": {
                        "missing": "organizeCustomPlan",
                        "user_message": "Review the drafted plan and reply approve.",
                        "reply_contract": reply_contract,
                    }
                }
            },
        }
    }

    out = _office_task_to_dispatch_data(
        office_response,
        {
            "task_id": "office-task-1",
            "service_url": "http://office-live:8040",
            "container_name": "office-task-live",
            "agent_id": "office",
        },
    )

    assert out["status"] == "input-required"
    assert out["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert out["taskId"] == "office-task-1"
    assert out["summary"] == "Office drafted an organize plan."
    assert out["question"] == "Review the drafted plan and reply approve."
    assert out["needs_clarification"]["missing"] == "organizeCustomPlan"
    assert out["needs_clarification"]["reply_contract"] == reply_contract


def test_compass_office_interrupt_kind_for_organize_dimension():
    from agents.compass.agent import _office_interrupt_kind

    office_request = {"capability": "organize"}
    payload = {"missing": "organizeGroupBy"}
    assert (
        _office_interrupt_kind(office_request, payload)
        == "office_organize_dimension"
    )

    # Generic missing field still maps to office_<field>.
    assert (
        _office_interrupt_kind({"capability": "summarize"}, {"missing": "locale"})
        == "office_locale"
    )

    # No missing key falls back to a generic slug.
    assert (
        _office_interrupt_kind({"capability": "summarize"}, {})
        == "office_clarification"
    )


def test_compass_resolve_office_resume_reply_for_organize_dimension():
    from agents.compass.agent import _resolve_office_resume_reply

    # Valid English id.
    resolved = _resolve_office_resume_reply(
        "office_organize_dimension", "size", {"capability": "organize"}
    )
    assert resolved.get("error_question", "") == ""
    office_request = resolved["office_request"]
    assert office_request["organize_dimension"] == "size"
    assert office_request["organize_metadata"]["organizeGroupBy"] == "size"

    # Valid Chinese keyword.
    resolved = _resolve_office_resume_reply(
        "office_organize_dimension", "按修改时间", {"capability": "organize"}
    )
    assert resolved["office_request"]["organize_dimension"] == "modified_time"

    # Invalid reply returns an error_question and a needs_clarification
    # payload for the next round-trip.
    resolved = _resolve_office_resume_reply(
        "office_organize_dimension", "students", {"capability": "organize"}
    )
    assert resolved["error_question"]
    payload = resolved["needs_clarification"]
    assert payload["missing"] == "organizeGroupBy"
    assert {opt["id"] for opt in payload["options"]} == {
        "size", "type", "created_time", "modified_time",
        "accessed_time", "filename",
    }


def test_compass_resolve_office_resume_reply_for_custom_plan_reask_preserves_plan():
    from agents.compass.agent import _resolve_office_resume_reply

    existing_plan = {
        "buckets": ["alpha", "beta"],
        "sample_mapping": {"one.txt": "alpha"},
        "classification_rule": "rule",
        "rationale": "why",
    }
    office_request = {
        "capability": "organize",
        "_needs_clarification": {
            "missing": "organizeCustomPlan",
            "options": [
                {"id": "approve", "label": "Approve plan"},
                {"id": "modify", "label": "Modify plan"},
            ],
            "user_message": "Review the plan and reply.",
            "plan": existing_plan,
            "plan_path": "/tmp/custom-organize-plan.md",
            "custom_hint": "student then month",
        },
    }

    resolved = _resolve_office_resume_reply(
        "office_organize_dimension",
        "not yet",
        office_request,
    )

    assert resolved["error_question"]
    payload = resolved["needs_clarification"]
    assert payload["missing"] == "organizeCustomPlan"
    assert payload["plan"] == existing_plan
    assert payload["plan_path"] == "/tmp/custom-organize-plan.md"
    assert payload["custom_hint"] == "student then month"


def test_compass_resolve_office_resume_reply_for_output_mode():
    from agents.compass.agent import _resolve_office_resume_reply

    resolved = _resolve_office_resume_reply(
        "office_output_mode", "workspace", {}
    )
    assert resolved["office_request"]["output_mode"] == "workspace"

    resolved = _resolve_office_resume_reply("office_output_mode", "garbage", {})
    assert resolved["error_question"]


# ---------------------------------------------------------------------------
# Compass dispatch tool: needs_clarification payload propagation
# ---------------------------------------------------------------------------


def test_dispatch_office_task_via_launcher_returns_clarification(monkeypatch, tmp_path):
    """The compass dispatch tool must surface the office task's
    ``_interrupt.needs_clarification`` payload in the dispatch result so
    that the surrounding compass flow can re-prompt the user.
    """
    from agents.compass import tools as compass_tools

    needs_clarification = {
        "missing": "organizeGroupBy",
        "options": [
            {"id": "size", "label": "size"},
            {"id": "type", "label": "type"},
        ],
        "user_message": "Office organize needs a grouping dimension.",
    }
    fake_task_dict = {
        "task": {
            "id": "office-clarify-1",
            "status": {
                "state": "TASK_STATE_INPUT_REQUIRED",
                "message": {
                    "parts": [{"text": "Office organize needs a grouping dimension."}],
                },
            },
            "artifacts": [],
            "metadata": {
                "_interrupt": {
                    "kind": "office_clarification",
                    "needs_clarification": needs_clarification,
                },
            },
        }
    }

    # Stub the launcher dispatch so the function never actually spawns a
    # container — the only thing under test is the wrapper that maps
    # the office task state into the compass-friendly payload.
    def _fake_dispatch_via_launcher(*args, **kwargs):
        return fake_task_dict

    monkeypatch.setattr(compass_tools, "dispatch_via_launcher", _fake_dispatch_via_launcher)
    monkeypatch.setattr(
        compass_tools,
        "_office_launch_definition",
        lambda capability: {"image": "fake-image"},
    )
    monkeypatch.setattr(
        compass_tools,
        "_office_mount_plan",
        lambda *args, **kwargs: {"translated_paths": ["/tmp/folder"], "env": {}, "extra_binds": []},
    )
    monkeypatch.setattr(
        compass_tools,
        "_build_office_dispatch_contract",
        lambda *args, **kwargs: ({}, {}),
    )
    monkeypatch.setattr(
        compass_tools,
        "_launcher_dispatch",
        type("L", (), {"get_launcher": staticmethod(lambda: None)}),
    )

    result = compass_tools._dispatch_office_task_via_launcher(
        task_description="please organize this folder",
        source_paths=["/tmp/folder"],
        output_mode="workspace",
        capability="organize",
        orchestrator_task_id="compass-test",
    )

    assert result["status"] == "input-required"
    assert result["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert result["needs_clarification"] == needs_clarification
    assert "Office organize needs a grouping dimension." in result["question"]


def test_dispatch_office_task_via_launcher_returns_completed_when_no_clarification(monkeypatch, tmp_path):
    from agents.compass import tools as compass_tools

    fake_task_dict = {
        "task": {
            "id": "office-ok-1",
            "status": {
                "state": "TASK_STATE_COMPLETED",
                "message": {"parts": [{"text": "Organized."}]},
            },
            "artifacts": [],
            "metadata": {},
        }
    }

    def _fake_dispatch_via_launcher(*args, **kwargs):
        return fake_task_dict

    monkeypatch.setattr(compass_tools, "dispatch_via_launcher", _fake_dispatch_via_launcher)
    monkeypatch.setattr(
        compass_tools,
        "_office_launch_definition",
        lambda capability: {"image": "fake-image"},
    )
    monkeypatch.setattr(
        compass_tools,
        "_office_mount_plan",
        lambda *args, **kwargs: {"translated_paths": ["/tmp/folder"], "env": {}, "extra_binds": []},
    )
    monkeypatch.setattr(
        compass_tools,
        "_build_office_dispatch_contract",
        lambda *args, **kwargs: ({}, {}),
    )
    monkeypatch.setattr(
        compass_tools,
        "_launcher_dispatch",
        type("L", (), {"get_launcher": staticmethod(lambda: None)}),
    )

    result = compass_tools._dispatch_office_task_via_launcher(
        task_description="please organize this folder by size",
        source_paths=["/tmp/folder"],
        output_mode="workspace",
        capability="organize",
        orchestrator_task_id="compass-test",
        organize_group_by="size",
    )
    assert result["status"] == "completed"
    assert result["needs_clarification"] == {}
    # The metadata forwarded to office must include organizeGroupBy when
    # supplied, otherwise the user-supplied dimension is lost.
    forwarded = result.get("forwarded_metadata")
    if forwarded is not None:
        assert forwarded.get("organizeGroupBy") == "size"


# ---------------------------------------------------------------------------
# End-to-end resume_task test
# ---------------------------------------------------------------------------


def test_compass_resume_task_for_organize_dimension_forwards_to_same_office_session(
    monkeypatch, tmp_path
):
    """A valid organize reply must be forwarded to the existing office session.

    Compass must not launch a second office agent for the same task.
    """
    from agents.compass.agent import CompassAgent, compass_definition
    from framework.task_store import InMemoryTaskStore

    task_store = InMemoryTaskStore()
    event_store = MagicMock()
    event_store.append = AsyncMock()
    services = AgentServices(
        session_service=MagicMock(),
        event_store=event_store,
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=MagicMock(),
        registry_client=None,
        task_store=task_store,
    )
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    # Pre-seed a task that is paused on the dimension clarification.
    task_id = "task-office-session-1"
    task = task_store.create_task(
        agent_id=compass_definition.agent_id,
        task_id=task_id,
        metadata={
            "task_type": "office",
            "user_request": "please organize this folder",
            "office_request": {
                "capability": "organize",
                "source_paths": ["/tmp/folder"],
                "output_mode": "workspace",
            },
            "office_session": {
                "task_id": task_id,
                "service_url": "http://office-live:8040",
                "container_name": "office-task-live",
                "agent_id": "office",
            },
        },
    )
    needs_clarification = {
        "missing": "organizeGroupBy",
        "options": [
            {"id": "size", "label": "size"},
            {"id": "type", "label": "type"},
        ],
        "user_message": "Office organize needs a grouping dimension.",
    }
    task_store.pause_task(
        task.id,
        question="Office organize needs a grouping dimension.",
        interrupt_metadata={
            "kind": "office_organize_dimension",
            "office_request": task.metadata["office_request"],
            "needs_clarification": needs_clarification,
        },
    )

    captured: dict = {}

    def _fake_resume_office_task(self, *, task_id, office_session, resume_value, office_request):
        captured["task_id"] = task_id
        captured["office_session"] = dict(office_session)
        captured["resume_value"] = resume_value
        captured["office_request"] = dict(office_request)

    class _InlineThread:
        def __init__(self, *, target=None, kwargs=None, daemon=None, name=None):
            self._target = target
            self._kwargs = kwargs or {}

        def start(self):
            if self._target:
                self._target(**self._kwargs)

    monkeypatch.setattr(
        "agents.compass.agent.CompassAgent._resume_office_task",
        _fake_resume_office_task,
    )
    monkeypatch.setattr("agents.compass.agent.threading.Thread", _InlineThread)

    result = asyncio.run(agent.resume_task(task.id, "size"))

    assert captured.get("office_request"), (
        "resume_task should have forwarded to the waiting office session"
    )
    office_request = captured["office_request"]
    assert captured["resume_value"] == "size"
    assert captured["office_session"]["service_url"] == "http://office-live:8040"
    assert office_request.get("organize_dimension") == "size"
    assert (
        (office_request.get("organize_metadata") or {}).get("organizeGroupBy")
        == "size"
    )
    ui = result.get("ui_update") or {}
    assert ui.get("task_id") == task.id
    assert ui.get("task_status") in {"TASK_STATE_WORKING", "TASK_STATE_SUBMITTED"}


def test_compass_resume_task_for_invalid_dimension_re_asks(monkeypatch, tmp_path):
    """When the user replies with an unrecognized value, ``resume_task``
    must keep the task in ``INPUT_REQUIRED`` and re-prompt with the
    needs_clarification options.
    """
    from agents.compass.agent import CompassAgent, compass_definition
    from framework.task_store import InMemoryTaskStore

    task_store = InMemoryTaskStore()
    services = AgentServices(
        session_service=MagicMock(),
        event_store=MagicMock(),
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=MagicMock(),
        registry_client=None,
        task_store=task_store,
    )
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    task = task_store.create_task(
        agent_id=compass_definition.agent_id,
        metadata={
            "task_type": "office",
            "user_request": "please organize this folder",
            "office_request": {
                "capability": "organize",
                "source_paths": ["/tmp/folder"],
                "output_mode": "workspace",
            },
        },
    )
    needs_clarification = {
        "missing": "organizeGroupBy",
        "options": [
            {"id": "size", "label": "size"},
            {"id": "type", "label": "type"},
        ],
        "user_message": "Office organize needs a grouping dimension.",
    }
    task_store.pause_task(
        task.id,
        question="Office organize needs a grouping dimension.",
        interrupt_metadata={
            "kind": "office_organize_dimension",
            "office_request": task.metadata["office_request"],
            "needs_clarification": needs_clarification,
        },
    )

    # Resume with garbage: the task should still be paused, not re-dispatched.
    asyncio.run(agent.resume_task(task.id, "students"))

    promoted = task_store.get_task(task.id)
    assert promoted.status.state == TaskState.INPUT_REQUIRED
    interrupt = (promoted.metadata or {}).get("_interrupt") or {}
    assert interrupt.get("kind") == "office_organize_dimension"
    # The new payload must still be a valid clarification with all
    # six options so the user can pick a real dimension.
    next_payload = interrupt.get("needs_clarification") or {}
    assert next_payload.get("missing") == "organizeGroupBy"
    assert {opt["id"] for opt in next_payload.get("options", [])} == {
        "size", "type", "created_time", "modified_time",
        "accessed_time", "filename",
    }


def test_compass_resume_task_for_custom_plan_approve_after_reask_forwards_plan_to_same_session(
    monkeypatch, tmp_path
):
    """If a custom-plan approval round re-asked the user first, a later
    ``approve`` must still forward the drafted plan to office instead of
    falling back to the planning phase again.
    """
    from agents.compass.agent import CompassAgent, compass_definition

    task_store = InMemoryTaskStore()
    services = AgentServices(
        session_service=MagicMock(),
        event_store=MagicMock(),
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=MagicMock(),
        registry_client=None,
        task_store=task_store,
    )
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    plan = {
        "buckets": ["alpha", "beta"],
        "sample_mapping": {"one.txt": "alpha"},
        "classification_rule": "rule",
        "rationale": "why",
    }
    office_request = {
        "capability": "organize",
        "source_paths": ["/tmp/folder"],
        "output_mode": "workspace",
        "organize_dimension": "__custom__",
        "organize_metadata": {
            "organizeGroupBy": "__custom__",
            "customDimensionHint": "student then month",
        },
        "_needs_clarification": {
            "missing": "organizeCustomPlan",
            "options": [
                {"id": "approve", "label": "Approve plan"},
                {"id": "modify", "label": "Modify plan"},
            ],
            "user_message": "Please reply with `approve` or `modify: <change>`.",
            "reply_contract": {
                "schema_version": 1,
                "kind": "approve_or_modify",
                "reask_message": "Please reply with `approve` or `modify: <change>`.",
            },
        },
    }
    task = task_store.create_task(
        agent_id=compass_definition.agent_id,
        metadata={
            "task_type": "office",
            "user_request": "please organize this folder by student then month",
            "office_request": office_request,
            "office_session": {
                "task_id": "task-custom-plan-1",
                "service_url": "http://office-live:8040",
                "container_name": "office-task-live",
                "agent_id": "office",
            },
        },
    )
    task_store.pause_task(
        task.id,
        question="Review the plan and reply.",
        interrupt_metadata={
            "kind": "office_organize_dimension",
            "office_request": office_request,
            "needs_clarification": {
                "missing": "organizeCustomPlan",
                "options": [
                    {"id": "approve", "label": "Approve plan"},
                    {"id": "modify", "label": "Modify plan"},
                ],
                "user_message": "Review the plan and reply.",
                "plan": plan,
                "plan_path": "/tmp/custom-organize-plan.md",
                "custom_hint": "student then month",
                "reply_contract": {
                    "schema_version": 1,
                    "kind": "approve_or_modify",
                    "reask_message": "Please reply with `approve` or `modify: <change>`.",
                },
            },
        },
    )

    captured: dict = {}

    def _fake_resume_office_task(self, *, task_id, office_session, resume_value, office_request):
        captured["task_id"] = task_id
        captured["office_session"] = dict(office_session)
        captured["resume_value"] = resume_value
        captured["office_request"] = dict(office_request)

    class _InlineThread:
        def __init__(self, *, target=None, kwargs=None, daemon=None, name=None):
            self._target = target
            self._kwargs = kwargs or {}

        def start(self):
            if self._target:
                self._target(**self._kwargs)

    monkeypatch.setattr(
        "agents.compass.agent.CompassAgent._resume_office_task",
        _fake_resume_office_task,
    )
    monkeypatch.setattr("agents.compass.agent.threading.Thread", _InlineThread)

    asyncio.run(agent.resume_task(task.id, "approve"))

    assert captured.get("office_request"), "resume_task should forward to the same office session"
    assert captured["resume_value"]["text"] == "approve"
    assert captured["resume_value"]["clarification_resolution"] == {
        "contract_kind": "approve_or_modify",
        "action": "approve",
        "note": "",
    }
    assert captured["office_session"]["service_url"] == "http://office-live:8040"
    forwarded = captured["office_request"]
    assert forwarded.get("clarification_resolution") == {
        "contract_kind": "approve_or_modify",
        "action": "approve",
        "note": "",
    }
    assert forwarded.get("organize_custom_plan") == plan


def test_compass_resume_task_for_custom_plan_modify_forwards_resolution_to_same_session(
    monkeypatch, tmp_path
):
    from agents.compass.agent import CompassAgent, compass_definition

    task_store = InMemoryTaskStore()
    services = AgentServices(
        session_service=MagicMock(),
        event_store=MagicMock(),
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=MagicMock(),
        registry_client=None,
        task_store=task_store,
    )
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    plan = {
        "buckets": ["January - Ethan"],
        "sample_mapping": {"one.txt": "January - Ethan"},
        "classification_rule": "rule",
        "rationale": "why",
    }
    office_request = {
        "capability": "organize",
        "source_paths": ["/tmp/folder"],
        "output_mode": "workspace",
        "organize_dimension": "__custom__",
        "organize_metadata": {
            "organizeGroupBy": "__custom__",
            "customDimensionHint": "student by month",
        },
        "_needs_clarification": {
            "missing": "organizeCustomPlan",
            "options": [
                {"id": "approve", "label": "Approve plan"},
                {"id": "modify", "label": "Modify plan"},
            ],
            "user_message": "Please reply with `approve` or `modify: <change>`.",
            "reply_contract": {
                "schema_version": 1,
                "kind": "approve_or_modify",
                "reask_message": "Please reply with `approve` or `modify: <change>`.",
            },
        },
    }
    task = task_store.create_task(
        agent_id=compass_definition.agent_id,
        metadata={
            "task_type": "office",
            "user_request": "please organize this folder by student then month",
            "office_request": office_request,
            "office_session": {
                "task_id": "task-custom-plan-modify-1",
                "service_url": "http://office-live:8040",
                "container_name": "office-task-live",
                "agent_id": "office",
            },
        },
    )
    task_store.pause_task(
        task.id,
        question="Review the plan and reply.",
        interrupt_metadata={
            "kind": "office_organize_dimension",
            "office_request": office_request,
            "needs_clarification": {
                "missing": "organizeCustomPlan",
                "options": [
                    {"id": "approve", "label": "Approve plan"},
                    {"id": "modify", "label": "Modify plan"},
                ],
                "user_message": "Review the plan and reply.",
                "plan": plan,
                "plan_path": "/tmp/custom-organize-plan.md",
                "custom_hint": "student by month",
                "reply_contract": {
                    "schema_version": 1,
                    "kind": "approve_or_modify",
                    "reask_message": "Please reply with `approve` or `modify: <change>`.",
                },
            },
        },
    )

    captured: dict[str, Any] = {}

    def _fake_resume_office_task(self, *, task_id, office_session, resume_value, office_request):
        captured["task_id"] = task_id
        captured["office_session"] = dict(office_session)
        captured["resume_value"] = resume_value
        captured["office_request"] = dict(office_request)

    class _InlineThread:
        def __init__(self, *, target=None, kwargs=None, daemon=None, name=None):
            self._target = target
            self._kwargs = kwargs or {}

        def start(self):
            if self._target:
                self._target(**self._kwargs)

    monkeypatch.setattr(
        "agents.compass.agent.CompassAgent._resume_office_task",
        _fake_resume_office_task,
    )
    monkeypatch.setattr("agents.compass.agent.threading.Thread", _InlineThread)

    reply = (
        "modify: create two level folder, first level folder use student name as "
        "folder name, under each student name folder, then create sub-folder "
        "using month name"
    )
    asyncio.run(agent.resume_task(task.id, reply))

    assert captured["resume_value"]["text"] == reply
    assert captured["resume_value"]["clarification_resolution"] == {
        "contract_kind": "approve_or_modify",
        "action": "modify",
        "note": (
            "create two level folder, first level folder use student name as "
            "folder name, under each student name folder, then create sub-folder "
            "using month name"
        ),
    }
    forwarded = captured["office_request"]
    assert forwarded["clarification_resolution"]["action"] == "modify"
    assert forwarded["organize_custom_modify_note"].startswith("create two level folder")
    assert forwarded["organize_custom_plan"] == plan


def test_office_resume_task_prefers_structured_clarification_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    from agents.office.agent import OfficeAgent, office_definition

    task_store = InMemoryTaskStore()
    agent = OfficeAgent(
        definition=office_definition,
        services=_agent_services(task_store=task_store),
    )
    asyncio.run(agent.start())

    captured: dict[str, Any] = {}

    def _fake_execute(*, state, **kwargs):
        captured["state"] = dict(state)
        task_store.complete_task(
            "office-task-contract-1",
            artifacts=[],
            message="done",
        )
        return task_store.get_task_dict("office-task-contract-1")

    monkeypatch.setattr(agent, "_execute_office_workflow", _fake_execute)

    request_metadata = {
        "organizeGroupBy": "__custom__",
        "customDimensionHint": "student then month",
        "executionContract": _make_execution_contract().to_dict(),
    }
    task = task_store.create_task(
        agent_id=office_definition.agent_id,
        task_id="office-task-contract-1",
        metadata={
            "user_text": "please organize this folder",
            "source_paths": ["/tmp/some/folder"],
            "capability": "organize",
            "output_mode": "workspace",
            "callback_url": "",
            "request_metadata": request_metadata,
        },
    )
    task_store.pause_task(
        task.id,
        question="Review the drafted plan and reply approve.",
        interrupt_metadata={
            "kind": "office_clarification",
            "needs_clarification": {
                "missing": "organizeCustomPlan",
                "plan": {"buckets": ["alpha"]},
                "reply_contract": {
                    "schema_version": 1,
                    "kind": "approve_or_modify",
                    "reask_message": "Please reply with `approve` or `modify: <change>`.",
                },
            },
        },
    )

    asyncio.run(agent.resume_task(task.id, {
        "text": "approve",
        "clarification_resolution": {
            "contract_kind": "approve_or_modify",
            "action": "approve",
            "note": "",
        },
    }))

    assert captured["state"]["organize_custom_action"] == "approve"


def test_office_resume_task_modify_replans_and_returns_revised_question(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    from agents.office.agent import OfficeAgent, office_definition

    task_store = InMemoryTaskStore()
    runtime = MagicMock()
    revised_plan = {
        "buckets": ["Ethan/January"],
        "sample_mapping": {"one.txt": "Ethan/January"},
        "classification_rule": "Group by student, then month.",
        "rationale": "revised",
    }
    runtime.run.return_value = {
        "summary": json.dumps(revised_plan),
        "raw_response": json.dumps(revised_plan),
    }
    plugin_manager = MagicMock()
    plugin_manager.fire = AsyncMock()
    event_store = MagicMock()
    event_store.append = AsyncMock()
    services = AgentServices(
        session_service=MagicMock(),
        event_store=event_store,
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=plugin_manager,
        checkpoint_service=InMemoryCheckpointer(),
        runtime=runtime,
        registry_client=None,
        task_store=task_store,
    )
    agent = OfficeAgent(
        definition=office_definition,
        services=services,
    )
    asyncio.run(agent.start())

    request_metadata = {
        "organizeGroupBy": "__custom__",
        "customDimensionHint": "student by month",
        "source_paths": [str(tmp_path)],
        "capability": "organize",
        "output_mode": "workspace",
        "executionContract": _make_execution_contract().to_dict(),
    }
    task = task_store.create_task(
        agent_id=office_definition.agent_id,
        task_id="office-task-contract-modify-1",
        metadata={
            "user_text": "please organize this folder by student by month",
            "source_paths": [str(tmp_path)],
            "capability": "organize",
            "output_mode": "workspace",
            "callback_url": "",
            "request_metadata": request_metadata,
        },
    )
    task_store.pause_task(
        task.id,
        question="Review the drafted plan and reply approve.",
        interrupt_metadata={
            "kind": "office_clarification",
            "needs_clarification": {
                "missing": "organizeCustomPlan",
                "plan": {
                    "buckets": ["January - Ethan"],
                    "sample_mapping": {"one.txt": "January - Ethan"},
                    "classification_rule": "Group by month and student.",
                    "rationale": "original",
                },
                "custom_hint": "student by month",
                "reply_contract": {
                    "schema_version": 1,
                    "kind": "approve_or_modify",
                    "reask_message": "Please reply with `approve` or `modify: <change>`.",
                },
            },
        },
    )

    result = asyncio.run(agent.resume_task(task.id, {
        "text": (
            "modify: create two level folder, first level folder use student "
            "name as folder name, under each student name folder, then create "
            "sub-folder using month name"
        ),
        "clarification_resolution": {
            "contract_kind": "approve_or_modify",
            "action": "modify",
            "note": (
                "create two level folder, first level folder use student "
                "name as folder name, under each student name folder, then "
                "create sub-folder using month name"
            ),
        },
    }))

    resumed = result["task"]
    assert resumed["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    question = resumed["status"]["message"]["parts"][0]["text"]
    assert "revised an organize plan" in question
    interrupt = resumed["metadata"]["_interrupt"]["needs_clarification"]
    assert interrupt["plan"] == revised_plan


# ---------------------------------------------------------------------------
# Regression: a re-dispatched handle_message with a modify request must
# not be silently swallowed by a stale workflow checkpoint.
# ---------------------------------------------------------------------------


def test_office_handle_message_modify_redispatch_uses_new_state(monkeypatch, tmp_path):
    """When a paused organize plan is re-dispatched via handle_message
    (the path compass uses when there is no live office session), the
    new ``organizeCustomAction`` / ``organizeCustomModifyNote`` must
    reach the planner.  The previous behaviour was for the workflow
    checkpoint from the first run to short-circuit the second run,
    so the user was re-prompted with the old plan instead of the
    revised one.
    """
    monkeypatch.setenv("OFFICE_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    from agents.office.agent import OfficeAgent, office_definition

    task_store = InMemoryTaskStore()
    checkpointer = InMemoryCheckpointer()
    runtime = MagicMock()
    original_plan = {
        "buckets": ["January - Ethan"],
        "sample_mapping": {"one.txt": "January - Ethan"},
        "classification_rule": "Group by month and student.",
        "rationale": "original",
    }
    revised_plan = {
        "buckets": ["Ethan/January"],
        "sample_mapping": {"one.txt": "Ethan/January"},
        "classification_rule": "Group by student, then month.",
        "rationale": "revised",
    }
    runtime.run.side_effect = [
        {
            "summary": json.dumps(original_plan),
            "raw_response": json.dumps(original_plan),
        },
        {
            "summary": json.dumps(revised_plan),
            "raw_response": json.dumps(revised_plan),
        },
    ]
    plugin_manager = MagicMock()
    plugin_manager.fire = AsyncMock()
    event_store = MagicMock()
    event_store.append = AsyncMock()
    services = AgentServices(
        session_service=MagicMock(),
        event_store=event_store,
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=plugin_manager,
        checkpoint_service=checkpointer,
        runtime=runtime,
        registry_client=None,
        task_store=task_store,
    )
    agent = OfficeAgent(
        definition=office_definition,
        services=services,
    )
    asyncio.run(agent.start())

    base_metadata = {
        "organizeGroupBy": "__custom__",
        "customDimensionHint": "student by month",
        "source_paths": [str(tmp_path)],
        "capability": "organize",
        "outputMode": "workspace",
        "executionContract": _make_execution_contract().to_dict(),
        "compassTaskId": "office-task-modify-redispatch",
    }

    # First dispatch: planner produces the original plan and the office
    # task pauses for user approval.
    first = asyncio.run(agent.handle_message(_build_message(base_metadata)))
    first_task = first["task"]
    first_task_id = first_task["id"]
    _wait_for_task_state(task_store, first_task_id, TaskState.INPUT_REQUIRED)
    assert runtime.run.call_count == 1

    # Second dispatch: same task id, but compass forwards the user's
    # modify reply.  The planner must be called again with the
    # modify note, and the revised plan must be the one the user is
    # asked to approve.
    modify_metadata = dict(base_metadata)
    modify_metadata["organizeCustomAction"] = "modify"
    modify_metadata["organizeCustomPlan"] = dict(original_plan)
    modify_metadata["organizeCustomModifyNote"] = (
        "create two level folder, first level folder use student name as "
        "folder name, under each student name folder, then create "
        "sub-folder using month name"
    )
    second = asyncio.run(agent.handle_message(_build_message(modify_metadata)))
    second_task = second["task"]
    _wait_for_task_state(task_store, first_task_id, TaskState.INPUT_REQUIRED)
    refreshed = task_store.get_task_dict(first_task_id)["task"]
    # The planner must have been called a second time with the modify note.
    assert runtime.run.call_count == 2
    second_call_prompt = runtime.run.call_args_list[1].args[0]
    assert "create two level folder" in second_call_prompt
    interrupt = refreshed["metadata"]["_interrupt"]["needs_clarification"]
    assert interrupt["plan"] == revised_plan
    question = refreshed["status"]["message"]["parts"][0]["text"]
    assert "revised an organize plan" in question


def _wait_for_task_state(task_store, task_id, expected_state, *, timeout: float = 5.0):
    """Poll the task store until the task reaches ``expected_state`` or
    ``timeout`` seconds elapse.  The office workflow runs in a
    background thread, so the test needs to wait for it.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = task_store.get_task(task_id)
        if task is not None:
            state_value = getattr(task.status.state, "value", str(task.status.state))
            if state_value == expected_state.value:
                return
            if state_value in {
                TaskState.COMPLETED.value,
                TaskState.FAILED.value,
                TaskState.CANCELLED.value,
            }:
                break
        time.sleep(0.05)
    task = task_store.get_task(task_id)
    actual = (
        getattr(task.status.state, "value", str(task.status.state))
        if task is not None
        else None
    )
    raise AssertionError(
        f"Task {task_id} did not reach {expected_state.value!r} "
        f"within {timeout}s (actual: {actual!r})"
    )


def _build_message(metadata: dict) -> dict:
    return {
        "message": {
            "parts": [
                {"text": "please organize this folder by student by month"},
            ],
            "metadata": dict(metadata),
        },
    }
