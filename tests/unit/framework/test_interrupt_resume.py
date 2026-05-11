"""Tests for interrupt → TASK_STATE_INPUT_REQUIRED → resume lifecycle."""
from __future__ import annotations

import asyncio

import pytest

from framework.a2a.protocol import TaskState
from framework.errors import InterruptSignal
from framework.task_store import InMemoryTaskStore
from framework.workflow import Workflow, START, END, RunConfig, interrupt
from framework.checkpoint import InMemoryCheckpointer


# ---------------------------------------------------------------------------
# Test nodes
# ---------------------------------------------------------------------------

async def step_a(state: dict) -> dict:
    return {"a_done": True}


async def step_b_interrupt(state: dict) -> dict:
    """This node always interrupts on first pass; continues on resume."""
    if state.get("_resume_value") is not None:
        return {"user_answer": state["_resume_value"], "b_done": True}
    interrupt(question="What is your preference?", topic="config")
    return {}  # unreachable


async def step_c(state: dict) -> dict:
    return {"c_done": True, "final": f"Answer was: {state.get('user_answer', '')}"}


# ---------------------------------------------------------------------------
# Workflow with interrupt
# ---------------------------------------------------------------------------

interrupt_workflow = Workflow(
    name="interrupt_test",
    edges=[
        (START, step_a, step_b_interrupt),
        (step_b_interrupt, step_c),
        (step_c, END),
    ],
)


class TestInterruptWorkflow:
    """Verify the framework-level interrupt / resume contract."""

    @pytest.fixture()
    def compiled(self):
        return interrupt_workflow.compile()

    @pytest.fixture()
    def checkpointer(self):
        return InMemoryCheckpointer()

    def test_interrupt_raises_signal(self, compiled, checkpointer):
        config = RunConfig(
            session_id="s1",
            thread_id="t1",
            checkpoint_service=checkpointer,
        )
        with pytest.raises(InterruptSignal) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                compiled.invoke({"input": "hello"}, config)
            )
        assert exc_info.value.question == "What is your preference?"
        assert exc_info.value.metadata.get("topic") == "config"

    def test_checkpoint_saved_on_interrupt(self, compiled, checkpointer):
        config = RunConfig(
            session_id="s2",
            thread_id="t2",
            checkpoint_service=checkpointer,
        )
        with pytest.raises(InterruptSignal):
            asyncio.get_event_loop().run_until_complete(
                compiled.invoke({"input": "hello"}, config)
            )
        # Verify checkpoint was persisted
        saved = asyncio.get_event_loop().run_until_complete(
            checkpointer.load("s2", "t2")
        )
        assert saved is not None
        assert saved["interrupt"] == "What is your preference?"

    def test_resume_completes_workflow(self, compiled, checkpointer):
        config = RunConfig(
            session_id="s3",
            thread_id="t3",
            checkpoint_service=checkpointer,
        )
        # First invoke — interrupts
        with pytest.raises(InterruptSignal):
            asyncio.get_event_loop().run_until_complete(
                compiled.invoke({"input": "hello"}, config)
            )

        # Resume with user answer
        result = asyncio.get_event_loop().run_until_complete(
            compiled.resume(config, resume_value="dark theme")
        )
        assert result.get("b_done") is True
        assert result.get("c_done") is True
        assert "dark theme" in result.get("final", "")


class TestTaskStoreInterruptState:
    """Verify TaskStore pause/resume transitions."""

    def test_pause_sets_input_required(self):
        store = InMemoryTaskStore()
        task = store.create_task(agent_id="test")
        # create_task already sets WORKING state
        store.pause_task(task.id, question="Need info")

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status.state == TaskState.INPUT_REQUIRED
        assert updated.status.message.parts[0]["text"] == "Need info"

    def test_resume_sets_working(self):
        store = InMemoryTaskStore()
        task = store.create_task(agent_id="test")
        # create_task already sets WORKING state
        store.pause_task(task.id, question="Need info")
        store.resume_task(task.id)

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status.state == TaskState.WORKING

    def test_pause_stores_interrupt_metadata(self):
        store = InMemoryTaskStore()
        task = store.create_task(agent_id="test")
        # create_task already sets WORKING state
        store.pause_task(task.id, question="Q?", interrupt_metadata={"topic": "auth"})

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.metadata.get("_interrupt", {}).get("topic") == "auth"


class TestA2AResumeEndpoint:
    """Verify the resume endpoint exists in A2ARequestHandler."""

    def test_resume_path_routing(self):
        """Smoke test that /tasks/{id}/resume is recognized in do_POST."""
        from framework.a2a.server import A2ARequestHandler
        # Verify the handler has _handle_resume method
        assert hasattr(A2ARequestHandler, "_handle_resume")
