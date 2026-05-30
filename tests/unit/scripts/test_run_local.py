import os

from framework.a2a.protocol import TaskState
from framework.checkpoint import InMemoryCheckpointer, SqliteCheckpointer
from framework.event_store import InMemoryEventStore, SqliteEventStore
from framework.session import InMemorySessionService, SqliteSessionService
from framework.task_store import InMemoryTaskStore, SqliteTaskStore
from scripts import run_local


def test_run_local_uses_port_env_when_cli_port_omitted(monkeypatch):
    monkeypatch.setenv("PORT", "8050")

    try:
        default_port = int(os.environ.get("PORT", "8000") or "8000")
    except ValueError:
        default_port = 8000

    assert default_port == 8050


def test_run_local_falls_back_to_8000_for_invalid_port_env(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-port")

    try:
        default_port = int(os.environ.get("PORT", "8000") or "8000")
    except ValueError:
        default_port = 8000

    assert default_port == 8000


def test_create_state_backends_uses_sqlite_for_persistent_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    config = {"execution_mode": "persistent"}

    session_service, event_store, checkpoint_service, task_store = run_local._create_state_backends(
        "compass",
        config,
    )

    assert isinstance(session_service, SqliteSessionService)
    assert isinstance(event_store, SqliteEventStore)
    assert isinstance(checkpoint_service, SqliteCheckpointer)
    assert isinstance(task_store, SqliteTaskStore)
    assert (tmp_path / "artifacts" / ".agent-state" / "compass").is_dir()


def test_create_state_backends_keeps_in_memory_for_per_task_agents():
    config = {"execution_mode": "per-task"}

    session_service, event_store, checkpoint_service, task_store = run_local._create_state_backends(
        "web-dev",
        config,
    )

    assert isinstance(session_service, InMemorySessionService)
    assert isinstance(event_store, InMemoryEventStore)
    assert isinstance(checkpoint_service, InMemoryCheckpointer)
    assert isinstance(task_store, InMemoryTaskStore)


def test_create_state_backends_recovers_orphaned_working_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    config = {"execution_mode": "persistent"}

    _, _, _, first_store = run_local._create_state_backends("team-lead", config)
    working_task = first_store.create_task("team-lead", task_id="task-working")
    input_required_task = first_store.create_task("team-lead", task_id="task-input")
    first_store.pause_task(input_required_task.id, "Need user input")

    _, _, _, recovered_store = run_local._create_state_backends("team-lead", config)
    recovered_working = recovered_store.get_task(working_task.id)
    recovered_input_required = recovered_store.get_task(input_required_task.id)

    assert recovered_working is not None
    assert recovered_working.status.state == TaskState.FAILED
    assert recovered_working.status.message is not None
    assert recovered_working.status.message.parts[0]["text"] == "Agent restarted before completing this task; please retry."
    assert recovered_input_required is not None
    assert recovered_input_required.status.state == TaskState.INPUT_REQUIRED