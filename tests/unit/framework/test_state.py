"""Tests for framework.state — Channel + Reducer semantics."""
import pytest

from framework.state import (
    Channel,
    StateSchema,
    WorkflowState,
    append_reducer,
    merge_reducer,
    merge_state,
)


class TestChannel:

    def test_default_channel_has_no_reducer(self):
        ch = Channel()
        assert ch.reducer is None

    def test_default_channel_default_is_none(self):
        ch = Channel()
        assert ch.default is None

    def test_channel_with_reducer_and_default(self):
        ch = Channel(reducer=append_reducer, default=[])
        assert ch.reducer is append_reducer
        assert ch.default == []


class TestAppendReducer:

    def test_list_plus_list(self):
        assert append_reducer([1, 2], [3, 4]) == [1, 2, 3, 4]

    def test_list_plus_single_item(self):
        assert append_reducer(["a"], "b") == ["a", "b"]

    def test_none_existing_with_list(self):
        assert append_reducer(None, [1, 2]) == [1, 2]

    def test_none_existing_with_single_item(self):
        assert append_reducer(None, "x") == ["x"]

    def test_empty_new_list(self):
        assert append_reducer([1], []) == [1]


class TestMergeReducer:

    def test_two_dicts(self):
        assert merge_reducer({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_overwrites_existing_sub_key(self):
        assert merge_reducer({"a": 1}, {"a": 99}) == {"a": 99}

    def test_none_existing(self):
        assert merge_reducer(None, {"x": 1}) == {"x": 1}

    def test_none_new(self):
        result = merge_reducer({"a": 1}, None)
        assert result == {"a": 1}


class TestMergeStateNoSchema:

    def test_simple_overwrite(self):
        base: WorkflowState = {"a": 1, "b": 2}
        merge_state(base, {"b": 99, "c": 3})
        assert base == {"a": 1, "b": 99, "c": 3}

    def test_returns_base(self):
        base: WorkflowState = {"x": 1}
        result = merge_state(base, {"y": 2})
        assert result is base

    def test_empty_delta(self):
        base: WorkflowState = {"a": 1}
        merge_state(base, {})
        assert base == {"a": 1}


class TestMergeStateWithSchema:

    def test_overwrite_channel_no_reducer(self):
        schema: StateSchema = {"notes": Channel()}
        base: WorkflowState = {"notes": "old"}
        merge_state(base, {"notes": "new"}, schema)
        assert base["notes"] == "new"

    def test_append_channel(self):
        schema: StateSchema = {
            "events": Channel(reducer=append_reducer, default=[]),
        }
        base: WorkflowState = {"events": ["a"]}
        merge_state(base, {"events": ["b", "c"]}, schema)
        assert base["events"] == ["a", "b", "c"]

    def test_missing_key_uses_default(self):
        schema: StateSchema = {
            "log": Channel(reducer=append_reducer, default=[]),
        }
        base: WorkflowState = {}
        merge_state(base, {"log": ["entry1"]}, schema)
        assert base["log"] == ["entry1"]

    def test_key_not_in_schema_overwrites(self):
        schema: StateSchema = {
            "tracked": Channel(reducer=append_reducer, default=[]),
        }
        base: WorkflowState = {"untracked": "old"}
        merge_state(base, {"untracked": "new", "tracked": ["x"]}, schema)
        assert base["untracked"] == "new"
        assert base["tracked"] == ["x"]

    def test_merge_reducer_channel(self):
        schema: StateSchema = {
            "metadata": Channel(reducer=merge_reducer, default={}),
        }
        base: WorkflowState = {"metadata": {"a": 1}}
        merge_state(base, {"metadata": {"b": 2}}, schema)
        assert base["metadata"] == {"a": 1, "b": 2}

    def test_merge_reducer_overwrites_sub_key(self):
        schema: StateSchema = {
            "metadata": Channel(reducer=merge_reducer, default={}),
        }
        base: WorkflowState = {"metadata": {"a": 1}}
        merge_state(base, {"metadata": {"a": 99}}, schema)
        assert base["metadata"]["a"] == 99

    def test_multiple_channels(self):
        schema: StateSchema = {
            "messages": Channel(reducer=append_reducer, default=[]),
            "status": Channel(),  # overwrite
        }
        base: WorkflowState = {"messages": ["hello"], "status": "idle"}
        merge_state(base, {"messages": ["world"], "status": "running"}, schema)
        assert base["messages"] == ["hello", "world"]
        assert base["status"] == "running"

    def test_none_schema_falls_back_to_overwrite(self):
        base: WorkflowState = {"a": 1}
        merge_state(base, {"a": 2}, schema=None)
        assert base["a"] == 2


class TestWorkflowWithStateSchema:
    """Integration: verify the workflow engine uses the schema when merging."""

    async def test_workflow_uses_append_reducer(self):
        from framework.workflow import Workflow, START, END
        from framework.state import Channel, append_reducer

        schema: StateSchema = {
            "log": Channel(reducer=append_reducer, default=[]),
        }

        async def step_a(state: dict) -> dict:
            return {"log": ["step_a"]}

        async def step_b(state: dict) -> dict:
            return {"log": ["step_b"]}

        wf = Workflow(
            name="log_test",
            edges=[
                (START, step_a, step_b),
                (step_b, END),
            ],
            state_schema=schema,
        )
        compiled = wf.compile()
        result = await compiled.invoke({"log": []})
        assert result["log"] == ["step_a", "step_b"]

    async def test_workflow_without_schema_overwrites(self):
        from framework.workflow import Workflow, START, END

        async def step_a(state: dict) -> dict:
            return {"value": "from_a"}

        async def step_b(state: dict) -> dict:
            return {"value": "from_b"}

        wf = Workflow(
            name="overwrite_test",
            edges=[
                (START, step_a, step_b),
                (step_b, END),
            ],
        )
        compiled = wf.compile()
        result = await compiled.invoke({"value": "initial"})
        assert result["value"] == "from_b"
