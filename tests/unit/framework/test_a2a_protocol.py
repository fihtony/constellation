"""Tests for framework.a2a.protocol — A2A protocol types and state machine."""
import pytest

from framework.a2a.protocol import (
    Artifact,
    Message,
    Task,
    TaskState,
    TaskStatus,
    validate_transition,
)
from framework.errors import InvalidTransitionError


class TestTaskStateMachine:

    def test_valid_transitions(self):
        """Valid state transitions should not raise."""
        validate_transition(TaskState.SUBMITTED, TaskState.WORKING)
        validate_transition(TaskState.WORKING, TaskState.COMPLETED)
        validate_transition(TaskState.WORKING, TaskState.FAILED)
        validate_transition(TaskState.WORKING, TaskState.INPUT_REQUIRED)
        validate_transition(TaskState.INPUT_REQUIRED, TaskState.WORKING)

    def test_invalid_transition_raises(self):
        """Invalid transitions should raise InvalidTransitionError."""
        with pytest.raises(InvalidTransitionError):
            validate_transition(TaskState.COMPLETED, TaskState.WORKING)

        with pytest.raises(InvalidTransitionError):
            validate_transition(TaskState.FAILED, TaskState.WORKING)

    def test_task_transition_method(self):
        task = Task()
        task.transition(TaskState.WORKING)
        assert task.status.state == TaskState.WORKING

        task.transition(TaskState.COMPLETED)
        assert task.status.state == TaskState.COMPLETED

    def test_task_to_dict(self):
        """Task.to_dict() should produce A2A wire format."""
        task = Task(id="test-001")
        task.transition(TaskState.WORKING)
        task.artifacts.append(Artifact(
            name="result",
            parts=[{"text": "done"}],
            metadata={"agentId": "test"},
        ))

        d = task.to_dict()
        assert d["task"]["id"] == "test-001"
        assert d["task"]["status"]["state"] == "TASK_STATE_WORKING"
        assert len(d["task"]["artifacts"]) == 1

    def test_task_from_dict(self):
        """Task.from_dict() should deserialize correctly."""
        raw = {
            "task": {
                "id": "task-abc",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [
                    {"name": "out", "artifactType": "text/plain", "parts": [{"text": "ok"}]},
                ],
            }
        }
        task = Task.from_dict(raw)
        assert task.id == "task-abc"
        assert task.status.state == TaskState.COMPLETED
        assert len(task.artifacts) == 1
        assert task.artifacts[0].name == "out"

    def test_input_required_state(self):
        """WORKING → INPUT_REQUIRED → WORKING should be valid."""
        task = Task()
        task.transition(TaskState.WORKING)
        task.transition(TaskState.INPUT_REQUIRED)
        task.transition(TaskState.WORKING)
        assert task.status.state == TaskState.WORKING

    def test_cancelled_state(self):
        """Most states should allow transition to CANCELLED."""
        task = Task()
        task.transition(TaskState.WORKING)
        task.transition(TaskState.CANCELLED)
        assert task.status.state == TaskState.CANCELLED

    def test_message_text(self):
        msg = Message(parts=[{"text": "hello "}, {"text": "world"}])
        assert msg.text() == "hello \nworld"
