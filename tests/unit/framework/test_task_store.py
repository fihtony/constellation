"""Tests for framework.task_store — InMemoryTaskStore and SqliteTaskStore."""
from __future__ import annotations

import os
import tempfile

import pytest

from framework.a2a.protocol import Artifact, TaskState
from framework.task_store import InMemoryTaskStore, SqliteTaskStore


# ---------------------------------------------------------------------------
# Shared test behaviour (parametrized for both backends)
# ---------------------------------------------------------------------------

@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryTaskStore()
    db_path = os.path.join(str(tmp_path), "test_tasks.db")
    return SqliteTaskStore(db_path=db_path)


class TestTaskStoreLifecycle:
    """Core lifecycle: create → update → complete/fail → query."""

    def test_create_and_get(self, store):
        task = store.create_task("agent-a")
        assert task.id
        assert task.status.state == TaskState.WORKING
        assert task.metadata.get("agentId") == "agent-a"

        fetched = store.get_task(task.id)
        assert fetched is not None
        assert fetched.id == task.id
        assert fetched.status.state == TaskState.WORKING

    def test_get_nonexistent(self, store):
        assert store.get_task("does-not-exist") is None

    def test_complete_task(self, store):
        task = store.create_task("agent-b")
        art = Artifact(name="out", parts=[{"text": "done"}])
        store.complete_task(task.id, artifacts=[art], message="All good")

        fetched = store.get_task(task.id)
        assert fetched.status.state == TaskState.COMPLETED
        assert len(fetched.artifacts) == 1
        assert fetched.artifacts[0].name == "out"

    def test_fail_task(self, store):
        task = store.create_task("agent-c")
        store.fail_task(task.id, error="Something broke")

        fetched = store.get_task(task.id)
        assert fetched.status.state == TaskState.FAILED

    def test_add_artifact(self, store):
        task = store.create_task("agent-d")
        store.add_artifact(task.id, Artifact(name="a1", parts=[{"text": "x"}]))
        store.add_artifact(task.id, Artifact(name="a2", parts=[{"text": "y"}]))

        fetched = store.get_task(task.id)
        assert len(fetched.artifacts) == 2
        names = {a.name for a in fetched.artifacts}
        assert names == {"a1", "a2"}

    def test_set_artifacts_replaces(self, store):
        task = store.create_task("agent-e")
        store.add_artifact(task.id, Artifact(name="old", parts=[{"text": "old"}]))
        store.set_artifacts(task.id, [Artifact(name="new", parts=[{"text": "new"}])])

        fetched = store.get_task(task.id)
        assert len(fetched.artifacts) == 1
        assert fetched.artifacts[0].name == "new"

    def test_update_metadata(self, store):
        task = store.create_task("agent-f", metadata={"key1": "val1"})
        store.update_metadata(task.id, {"key2": "val2"})

        fetched = store.get_task(task.id)
        assert fetched.metadata.get("key1") == "val1"
        assert fetched.metadata.get("key2") == "val2"

    def test_list_tasks_all(self, store):
        store.create_task("agent-g")
        store.create_task("agent-g")
        store.create_task("agent-h")

        all_tasks = store.list_tasks()
        assert len(all_tasks) == 3

    def test_list_tasks_by_agent(self, store):
        store.create_task("agent-i")
        store.create_task("agent-i")
        store.create_task("agent-j")

        filtered = store.list_tasks(agent_id="agent-i")
        assert len(filtered) == 2

    def test_list_tasks_by_state(self, store):
        t1 = store.create_task("agent-k")
        t2 = store.create_task("agent-k")
        store.complete_task(t1.id)

        completed = store.list_tasks(state=TaskState.COMPLETED)
        assert len(completed) == 1
        assert completed[0].id == t1.id

    def test_get_task_dict_format(self, store):
        task = store.create_task("agent-l")
        d = store.get_task_dict(task.id)
        assert "task" in d
        assert d["task"]["id"] == task.id
        assert d["task"]["status"]["state"] == "TASK_STATE_WORKING"

    def test_get_task_dict_nonexistent(self, store):
        d = store.get_task_dict("does-not-exist")
        assert d["task"]["status"]["state"] == "TASK_STATE_FAILED"

    def test_list_tasks_with_limit(self, store):
        for i in range(5):
            store.create_task(f"agent-m")
        result = store.list_tasks(limit=3)
        assert len(result) == 3
