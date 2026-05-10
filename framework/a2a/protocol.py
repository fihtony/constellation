"""A2A protocol types and task state machine.

Defines the core data structures for inter-agent communication: Task, Message,
Artifact, and the TaskState enum with validated transitions.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from framework.errors import InvalidTransitionError


class TaskState(str, Enum):
    """Lifecycle states of an A2A task."""

    SUBMITTED = "SUBMITTED"
    ROUTING = "ROUTING"
    DISPATCHED = "DISPATCHED"
    WORKING = "TASK_STATE_WORKING"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    CANCELLED = "TASK_STATE_CANCELLED"


# Valid state transitions
_VALID_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.SUBMITTED: {TaskState.ROUTING, TaskState.WORKING, TaskState.CANCELLED},
    TaskState.ROUTING: {TaskState.DISPATCHED, TaskState.WORKING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.DISPATCHED: {TaskState.WORKING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.WORKING: {
        TaskState.COMPLETED, TaskState.FAILED,
        TaskState.INPUT_REQUIRED, TaskState.CANCELLED,
    },
    TaskState.INPUT_REQUIRED: {TaskState.WORKING, TaskState.CANCELLED},
    # Terminal states — no transitions out
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
}


def validate_transition(current: TaskState, target: TaskState) -> None:
    """Raise InvalidTransitionError if the transition is not allowed."""
    allowed = _VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(
            f"Cannot transition from {current.value} to {target.value}"
        )


@dataclass
class Message:
    """An A2A message (user or agent role)."""

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: str = "ROLE_USER"
    parts: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def text(self) -> str:
        """Concatenate all text parts."""
        return "\n".join(p.get("text", "") for p in self.parts if "text" in p)


@dataclass
class TaskStatus:
    """Current status of a task."""

    state: TaskState = TaskState.SUBMITTED
    message: Message | None = None


@dataclass
class Artifact:
    """An output artifact produced by an agent."""

    name: str
    artifact_type: str = "text/plain"
    parts: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class Task:
    """An A2A task with state, artifacts, and metadata."""

    id: str = field(default_factory=lambda: f"task-{uuid.uuid4().hex[:12]}")
    status: TaskStatus = field(default_factory=TaskStatus)
    artifacts: list[Artifact] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def transition(self, target: TaskState, message: Message | None = None) -> None:
        """Transition to a new state with optional status message."""
        validate_transition(self.status.state, target)
        self.status.state = target
        if message:
            self.status.message = message

    def to_dict(self) -> dict:
        """Serialize to the A2A wire format."""
        status_dict: dict[str, Any] = {"state": self.status.state.value}
        if self.status.message:
            status_dict["message"] = {
                "messageId": self.status.message.message_id,
                "role": self.status.message.role,
                "parts": self.status.message.parts,
            }
        return {
            "task": {
                "id": self.id,
                "status": status_dict,
                "artifacts": [
                    {
                        "name": a.name,
                        "artifactType": a.artifact_type,
                        "parts": a.parts,
                        "metadata": a.metadata,
                    }
                    for a in self.artifacts
                ],
                "metadata": self.metadata,
            }
        }

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        """Deserialize from A2A wire format."""
        td = data.get("task", data)
        status_data = td.get("status", {})
        status = TaskStatus(state=TaskState(status_data.get("state", "SUBMITTED")))
        msg_data = status_data.get("message")
        if msg_data:
            status.message = Message(
                message_id=msg_data.get("messageId", ""),
                role=msg_data.get("role", "ROLE_USER"),
                parts=msg_data.get("parts", []),
                metadata=msg_data.get("metadata", {}),
            )
        artifacts = [
            Artifact(
                name=a["name"],
                artifact_type=a.get("artifactType", "text/plain"),
                parts=a.get("parts", []),
                metadata=a.get("metadata", {}),
            )
            for a in td.get("artifacts", [])
        ]
        return cls(
            id=td.get("id", f"task-{uuid.uuid4().hex[:12]}"),
            status=status,
            artifacts=artifacts,
            metadata=td.get("metadata", {}),
        )
