"""Tests for framework.workflow — Workflow engine."""
import pytest

from framework.errors import InterruptSignal, MaxStepsExceeded
from framework.checkpoint import InMemoryCheckpointer
from framework.workflow import END, START, RunConfig, Workflow, interrupt


# ---------------------------------------------------------------------------
# Node functions for tests
# ---------------------------------------------------------------------------

async def step_a(state):
    return {**state, "a": True}

async def step_b(state):
    return {**state, "b": True}

async def step_c(state):
    return {**state, "c": True}

async def router_hi_lo(state):
    return {"route": "high" if state.get("value", 0) > 5 else "low"}

async def path_high(state):
    return {**state, "path": "high"}

async def path_low(state):
    return {**state, "path": "low"}

async def increment(state):
    count = state.get("count", 0) + 1
    return {"count": count, "route": "done" if count >= 3 else "retry"}

async def infinite_loop(state):
    return {"route": "loop"}

async def ask_user(state):
    if "_resume_value" in state:
        return {"jira_key": state["_resume_value"]}
    interrupt("What is the Jira ticket?")

async def use_ticket(state):
    return {"result": f"Working on {state['jira_key']}"}


# ---------------------------------------------------------------------------
# Basic graph tests
# ---------------------------------------------------------------------------

class TestWorkflowBasic:
    """Test basic workflow construction and execution."""

    @pytest.mark.asyncio
    async def test_linear_workflow(self):
        """Three-step linear: START → A → B → C → END."""
        wf = Workflow(name="linear", edges=[
            (START, step_a, step_b),
            (step_b, step_c),
            (step_c, END),
        ])
        compiled = wf.compile()
        result = await compiled.invoke({"input": "test"})

        assert result["a"] is True
        assert result["b"] is True
        assert result["c"] is True

    @pytest.mark.asyncio
    async def test_conditional_routing(self):
        """Conditional: route based on value > 5."""
        wf = Workflow(name="conditional", edges=[
            (START, router_hi_lo, {
                "high": path_high,
                "low": path_low,
            }),
            (path_high, END),
            (path_low, END),
        ])
        compiled = wf.compile()

        result_low = await compiled.invoke({"value": 3})
        assert result_low["path"] == "low"

        result_high = await compiled.invoke({"value": 10})
        assert result_high["path"] == "high"

    @pytest.mark.asyncio
    async def test_loop_workflow(self):
        """Loop: increment until count >= 3."""
        wf = Workflow(name="loop", edges=[
            (START, increment, {
                "retry": increment,
                "done": END,
            }),
        ])
        compiled = wf.compile()
        result = await compiled.invoke({})

        assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_max_steps_guard(self):
        """Should raise MaxStepsExceeded when exceeding max_steps."""
        wf = Workflow(name="infinite", edges=[
            (START, infinite_loop, {"loop": infinite_loop}),
        ])
        compiled = wf.compile()

        with pytest.raises(MaxStepsExceeded, match="max_steps"):
            await compiled.invoke({}, RunConfig(max_steps=10))


# ---------------------------------------------------------------------------
# Interrupt / Resume tests
# ---------------------------------------------------------------------------

class TestWorkflowInterrupt:
    """Test human-in-the-loop interrupt and resume."""

    @pytest.mark.asyncio
    async def test_interrupt_pauses_workflow(self):
        """interrupt() should pause the workflow."""
        wf = Workflow(name="interrupt_test", edges=[
            (START, ask_user, END),
        ])
        compiled = wf.compile()

        with pytest.raises(InterruptSignal) as exc_info:
            await compiled.invoke({})

        assert exc_info.value.question == "What is the Jira ticket?"

    @pytest.mark.asyncio
    async def test_resume_continues_workflow(self):
        """resume() should continue from the interrupt point."""
        checkpoint = InMemoryCheckpointer()

        wf = Workflow(name="resume_test", edges=[
            (START, ask_user, use_ticket),
            (use_ticket, END),
        ])
        compiled = wf.compile()
        config = RunConfig(
            session_id="s1",
            thread_id="t1",
            checkpoint_service=checkpoint,
        )

        # First run: should interrupt
        with pytest.raises(InterruptSignal):
            await compiled.invoke({}, config)

        # Resume with user input
        result = await compiled.resume(config, "JIRA-123")
        assert result["jira_key"] == "JIRA-123"
        assert "Working on JIRA-123" in result["result"]


# ---------------------------------------------------------------------------
# Checkpoint tests
# ---------------------------------------------------------------------------

class TestWorkflowCheckpoint:
    """Test checkpoint save/restore."""

    @pytest.mark.asyncio
    async def test_checkpoint_saves_after_each_step(self):
        """Checkpoint should be saved after each node execution."""
        checkpoint = InMemoryCheckpointer()

        wf = Workflow(name="ckpt", edges=[
            (START, step_a, step_b),
            (step_b, END),
        ])
        compiled = wf.compile()

        await compiled.invoke(
            {"input": "test"},
            RunConfig(session_id="s1", thread_id="t1", checkpoint_service=checkpoint),
        )

        # After completion, checkpoint should exist
        saved = await checkpoint.load("s1", "t1")
        assert saved is not None
        assert saved["state"]["a"] is True
        assert saved["state"]["b"] is True
