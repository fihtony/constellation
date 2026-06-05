"""Tests for the dimension-first gate in the office request path.

The user reported task-440f61c09ffa ("please organize folder in
/Users/aibot/projects/constellation/tests/data/2026 by student name")
got stuck: compass asked for output_mode, the user replied "workspace",
the office dispatched, but office asked for a dimension because "by
student name" does not match any of the 6 supported dimensions.

Two changes pinned here:

1. Compass must ask the dimension question BEFORE the output_mode
   question for organize requests that have no matching dimension.
   This collapses the typical "ask workspace, then ask dimension"
   two-round trip into a single round for the common case.

2. The dimension prompt must explicitly call out when the user's
   natural-language hint does not match any of the 6 supported
   dimensions.  For "by student name" the prompt should name the
   unsupported hint and list the 6 valid dimensions, so the user
   can pick one without re-asking.
"""

from __future__ import annotations

import asyncio
import os
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


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_office_dimension_resolved_true_for_keyword_in_user_text():
    from agents.compass.agent import _office_dimension_resolved

    assert _office_dimension_resolved("organize by file size", {"capability": "organize"})
    assert _office_dimension_resolved("organize by type", {"capability": "organize"})
    assert _office_dimension_resolved("按文件大小整理", {"capability": "organize"})


def test_office_dimension_resolved_false_for_unmatched_hint():
    from agents.compass.agent import _office_dimension_resolved

    # The original failing scenario: "by student name" does not match
    # any of the 6 supported dimensions.
    assert not _office_dimension_resolved(
        "please organize by student name", {"capability": "organize"}
    )
    # Other non-matching hints.
    assert not _office_dimension_resolved(
        "please organize by color", {"capability": "organize"}
    )
    assert not _office_dimension_resolved(
        "请按颜色整理", {"capability": "organize"}
    )


def test_office_organize_dimension_question_for_unmatched_hint():
    """The dimension prompt must explicitly call out the unsupported
    hint so the user sees *why* their phrasing was rejected and can
    pick a supported one without further round-trips.
    """
    from agents.compass.agent import _office_organize_dimension_question

    question = _office_organize_dimension_question("please organize by student name")
    assert "student" in question or "by student" in question, (
        f"prompt should name the unsupported hint: {question!r}"
    )
    # All 6 valid dimensions are listed.
    for dim in ("size", "type", "created_time", "modified_time", "accessed_time", "filename"):
        assert f"`{dim}`" in question, f"missing dimension {dim!r} in: {question!r}"


def test_office_organize_dimension_question_for_ambiguous_text():
    from agents.compass.agent import _office_organize_dimension_question

    question = _office_organize_dimension_question("please organize this folder")
    # The neutral prompt still lists the 6 options.
    for dim in ("size", "type", "created_time", "modified_time", "accessed_time", "filename"):
        assert f"`{dim}`" in question


# ---------------------------------------------------------------------------
# Integration: dimension gate fires before output_mode gate
# ---------------------------------------------------------------------------


@pytest.fixture()
def compass_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    from agents.compass.agent import CompassAgent, compass_definition

    task_store = InMemoryTaskStore()
    services = _agent_services(task_store=task_store)
    agent = CompassAgent(definition=compass_definition, services=services)
    asyncio.run(agent.start())

    # Stub the dispatch path so the agent does not actually try to
    # launch an office container.
    async def _no_complete(self, **kwargs):
        return None
    monkeypatch.setattr(
        "agents.compass.agent.CompassAgent._complete_office_task",
        _no_complete,
    )

    class _StubAgent:
        async def handle_message(self, msg):
            return await asyncio.create_task(
                asyncio.coroutine(lambda: _StubAgent._real(msg))()
            )

        @staticmethod
        async def _real(msg):
            return None

    return agent, task_store


async def _send(agent, text, *, metadata=None):
    """Drive handle_message for a single office request."""
    from framework.a2a.protocol import Artifact
    from framework.devlog import AgentLogger
    from framework.tools.registry import get_registry

    register_compass_tools = MagicMock()
    # The handle_message method is async; invoke it directly.
    msg = {
        "message": {
            "parts": [{"text": text}],
            "metadata": metadata or {},
        }
    }
    return await agent.handle_message(msg)


def test_organize_by_student_name_triggers_dimension_not_output_mode(compass_agent):
    """The exact scenario from task-440f61c09ffa: 'please organize
    folder ... by student name' should land in INPUT_REQUIRED with
    kind=office_organize_dimension (NOT output_mode), and the
    question should name the unsupported hint.
    """
    from agents.compass.tools import register_compass_tools as _reg  # noqa: F401

    agent, task_store = compass_agent

    result = asyncio.run(agent.handle_message({
        "message": {
            "parts": [{
                "text": "please organize folder in /Users/aibot/projects/constellation/tests/data/2026 by student name"
            }],
            "metadata": {},
        }
    }))

    ui = result.get("ui_update") or {}
    assert ui.get("task_status") == "TASK_STATE_INPUT_REQUIRED"
    question = (ui.get("chat_message") or {}).get("text", "")
    assert "size" in question, f"dimension list missing in: {question!r}"
    # The prompt should mention that "by student name" is not one of
    # the supported dimensions, so the user knows *why* they're being
    # re-asked.
    assert "student" in question.lower(), (
        f"prompt should name the unsupported hint: {question!r}"
    )
    # The compass task was paused with the dimension interrupt, NOT
    # the output_mode one.
    task_id = ui.get("task_id")
    promoted = task_store.get_task(task_id)
    interrupt = (promoted.metadata or {}).get("_interrupt") or {}
    assert interrupt.get("kind") == "office_organize_dimension", (
        f"expected dimension gate first, got: {interrupt.get('kind')!r}"
    )


def test_organize_with_dim_and_output_mode_dispatches_immediately(compass_agent):
    """Sanity check: when the user provides both a dimension hint and
    an output-mode hint, the office task must dispatch immediately
    with no clarifying questions.
    """
    from agents.compass.agent import CompassAgent, compass_definition  # noqa: F401

    agent, task_store = compass_agent

    # Stub the dispatcher so we can capture the office_request that
    # compass passes to the launcher.
    captured: dict = {}

    def _fake_dispatch_office_request(task_id, user_text, office_request, registry, log):
        captured["office_request"] = dict(office_request)
        return {"status": "completed", "state": "TASK_STATE_COMPLETED"}

    import agents.compass.agent as _cm
    _cm._dispatch_office_request = _fake_dispatch_office_request

    result = asyncio.run(agent.handle_message({
        "message": {
            "parts": [{
                "text": "please organize folder in /data/2026 by file size in workspace"
            }],
            "metadata": {},
        }
    }))

    assert captured.get("office_request"), "office should have been dispatched"
    assert captured["office_request"]["capability"] == "organize"
    assert captured["office_request"]["output_mode"] == "workspace"
    # The user_text passed "by file size" → "size" — but the metadata
    # is filled in by office's parse_dimension, not by compass.
    # The important thing is that compass did not pause the task.
    ui = result.get("ui_update") or {}
    assert ui.get("task_status") != "TASK_STATE_INPUT_REQUIRED", (
        f"task should have dispatched, got: {ui}"
    )
