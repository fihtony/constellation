"""Constellation Framework — core infrastructure for multi-agent workflows."""

__version__ = "2.0.0"

from framework.major_step import (  # noqa: F401  # public re-export
    LIFECYCLE_CANCELLED,
    LIFECYCLE_CONDITIONAL_PENDING,
    LIFECYCLE_DONE,
    LIFECYCLE_FAILED,
    LIFECYCLE_PENDING,
    LIFECYCLE_RESUMING,
    LIFECYCLE_RUNNING,
    LIFECYCLE_TERMINATED,
    LIFECYCLE_WAITING_FOR_USER,
    LIFECYCLE_WARNING,
    LIFECYCLE_STATES,
    TERMINAL_LIFECYCLE_STATES,
    VISUAL_CONDITIONAL_PENDING,
    VISUAL_CURRENT,
    VISUAL_DONE,
    VISUAL_FAILED,
    VISUAL_PENDING,
    VISUAL_STATES,
    VISUAL_WARN,
    HttpMajorStepSink,
    InProcessMajorStepSink,
    MajorStepSink,
    NullMajorStepSink,
    default_visual_state,
    ensure_major_step_skeleton,
    record_major_step,
    resolve_progress_sink,
)
