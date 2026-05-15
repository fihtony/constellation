"""Framework-level exception hierarchy."""
from __future__ import annotations

from typing import Optional


class ConstellationError(Exception):
    """Base exception for all Constellation framework errors."""


class WorkflowError(ConstellationError):
    """Errors during workflow compilation or execution."""


class MaxStepsExceeded(WorkflowError):
    """Workflow exceeded the configured max_steps limit."""


class InterruptSignal(WorkflowError):
    """Raised by a node to pause the workflow and request external input.

    Attributes:
        question: human-readable description of what input is needed.
        metadata: optional dict for structured interrupt context.
    """

    def __init__(self, question: str, metadata: Optional[dict] = None):
        super().__init__(question)
        self.question = question
        self.metadata = metadata or {}


class SessionError(ConstellationError):
    """Errors in session management."""


class SessionNotFoundError(SessionError):
    """Requested session does not exist."""


class CheckpointError(ConstellationError):
    """Errors in checkpoint save/load."""


class CheckpointNotFoundError(CheckpointError):
    """No checkpoint exists for the given key."""


class SkillError(ConstellationError):
    """Errors loading or resolving skills."""


class PluginError(ConstellationError):
    """Errors in plugin lifecycle."""


class PermissionDeniedError(ConstellationError):
    """An operation was blocked by the permission engine."""


class A2AError(ConstellationError):
    """Errors in A2A protocol communication."""


class TaskNotFoundError(A2AError):
    """Requested task does not exist."""


class InvalidTransitionError(A2AError):
    """Attempted an invalid task state transition."""


class RuntimeError_(ConstellationError):
    """Errors in the agent runtime adapter layer."""


class ToolError(ConstellationError):
    """Errors during tool execution."""
