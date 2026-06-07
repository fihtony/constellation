"""Office Agent — major-step recording helpers.

The Office agent's workflow has 4 outer graph nodes (``receive_task``,
``analyze_request``, ``execute_office_work``, ``report_result``) and one
opaque ReAct core (``execute_office_work``). The major-step timeline is
emitted at these boundary nodes plus via per-tool hooks; this module is the
single place where Office-specific step keys and template strings are
defined.

Office tasks have **capability-specific** skeletons per design doc §3.2:
- ``analyze``    — 8 rows
- ``summarize``  — 8 rows (9 if combining is needed)
- ``organize``   — 9 rows

The ReAct core is still opaque, but the emitted rows now use proposal-aligned
step keys/titles so the timeline reads like a real workflow instead of a
generic "executing capability" placeholder.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from framework.major_step import (
    LIFECYCLE_DONE,
    LIFECYCLE_FAILED,
    LIFECYCLE_RUNNING,
    LIFECYCLE_WARNING,
    record_major_step,
)

logger = logging.getLogger(__name__)


def _resolve_task_id(state: dict) -> str:
    """Return the Office task id (which may be the Compass task id when shared)."""
    return (
        state.get("_compass_task_id")
        or state.get("_task_id")
        or state.get("task_id")
        or ""
    )


def _source_kind(capability: str, count: int) -> str:
    if capability == "organize":
        return "folder" if count == 1 else "folders"
    return "file" if count == 1 else "files"


def _kind_for_paths(paths: list[str]) -> str:
    count = len(paths)
    if count <= 0:
        return "files"
    directory_count = sum(1 for path in paths if path and os.path.isdir(path))
    if directory_count == count:
        return "folder" if count == 1 else "folders"
    if directory_count == 0:
        return "file" if count == 1 else "files"
    return "source" if count == 1 else "sources"


def _count_and_kind_from_paths(
    capability: str,
    paths: Any,
    *,
    prefer_path_types: bool = False,
) -> tuple[int, str]:
    normalized_paths = [str(path) for path in paths] if isinstance(paths, list) else []
    count = len(normalized_paths)
    if prefer_path_types:
        return count, _kind_for_paths(normalized_paths)
    return count, _source_kind(capability, count)


def _output_location_for_state(state: dict) -> str:
    output_mode = state.get("output_mode", "workspace")
    return "the workspace" if output_mode == "workspace" else "the source folder"


def check_office_cancel(state: dict) -> None:
    """Raise :class:`agents.office.agent._CancelWorkflow` if the user
    requested a cancel on the running task.

    Office public node functions call this helper at the top of their
    body so a long-running workflow exits promptly at the next node
    boundary. The cancel event itself is wired up by
    :class:`agents.office.agent.OfficeAgent` before the workflow
    starts; here we only read the value from state.
    """
    # Local import avoids a circular dependency between
    # ``office_steps`` and ``office.agent``.
    from agents.office.agent import _CancelWorkflow

    event = state.get("_cancel_event")
    if event is not None and event.is_set():
        raise _CancelWorkflow()


def _execution_step_for_capability(capability: str, source_count: int) -> tuple[str, str, str, dict]:
    if capability == "analyze":
        return (
            "office.inferring_schema",
            "Office inferring data schema",
            "Office inferred the data schema for {source_count} file(s).",
            {"source_count": source_count},
        )
    if capability == "organize":
        return (
            "office.scanning",
            "Office scanning folder structure",
            "Office scanned the folder structure for {source_count} {source_kind}.",
            {
                "source_count": source_count,
                "source_kind": _source_kind(capability, source_count),
            },
        )
    return (
        "office.reading",
        "Office reading documents",
        "Office read {source_count} {source_kind} via MCP tools.",
        {
            "source_count": source_count,
            "source_kind": _source_kind(capability, source_count),
        },
    )


def record_office_step(
    state: dict,
    *,
    step_key: str,
    title: str,
    lifecycle_state: str = LIFECYCLE_RUNNING,
    summary_template: str = "",
    summary_facts: dict | None = None,
    conditional: bool = False,
    round: int = 0,
) -> None:
    """Append a major-step event for the current Office task.

    The Office agent runs in the same process as the office container (or
    in-process for tests); ``orchestrator_task_id`` is the Compass task id
    carried on ``state["_compass_task_id"]``. The local ``TaskStore`` is
    the source of truth for the Office task; the orchestrator task is
    updated via the same store (Compass and Office share it on the same
    container) or via the ``progress_sink`` for cross-container cases.
    """
    task_id = _resolve_task_id(state)
    if not task_id:
        return
    task_store = state.get("_task_store")
    orchestrator_task_id = state.get("_compass_task_id") or task_id
    progress_sink = state.get("_major_step_progress_sink")
    try:
        record_major_step(
            task_id,
            step_key=step_key,
            title=title,
            agent="office",
            lifecycle_state=lifecycle_state,
            summary_template=summary_template,
            summary_facts=summary_facts,
            conditional=conditional,
            round=round,
            orchestrator_task_id=orchestrator_task_id,
            progress_sink=progress_sink,
            task_store=task_store,
        )
    except Exception as exc:  # noqa: BLE001 - never block workflow on step writes
        logger.debug("[office-steps] record_major_step failed: %s", exc)


def emit_received(
    state: dict,
    *,
    lifecycle_state: str = LIFECYCLE_RUNNING,
) -> None:
    capability = state.get("capability", "summarize")
    source_paths = state.get("source_paths", [])
    count, source_kind = _count_and_kind_from_paths(
        capability,
        source_paths,
        prefer_path_types=True,
    )
    discovered_source_count = int(state.get("discovered_source_count") or 0)
    summary_template = "Office received the task: {capability} on {source_count} {source_kind}."
    if source_kind in {"folder", "folders"} and discovered_source_count > 0:
        summary_template = (
            "Office received the task: {capability} on {source_count} {source_kind} "
            "containing {discovered_source_count} file(s)."
        )
    record_office_step(
        state,
        step_key="office.received",
        title="Office receiving task",
        lifecycle_state=lifecycle_state,
        summary_template=summary_template,
        summary_facts={
            "capability": capability,
            "source_count": count,
            "source_kind": source_kind,
            "discovered_source_count": discovered_source_count,
        },
    )


def emit_validating(
    state: dict,
    *,
    lifecycle_state: str = LIFECYCLE_RUNNING,
) -> None:
    capability = state.get("capability", "summarize")
    source_paths = state.get("source_paths", [])
    count, source_kind = _count_and_kind_from_paths(capability, source_paths)
    record_office_step(
        state,
        step_key="office.validating",
        title="Office validating sources and permissions",
        lifecycle_state=lifecycle_state,
        summary_template="Office validated {source_count} {source_kind} and prepared the output area.",
        summary_facts={
            "source_count": count,
            "source_kind": source_kind,
        },
    )


def emit_executing_capability(state: dict) -> None:
    """Emit the capability summary row at the start of ``execute_office_work``.

    This row covers the inner steps the LLM does opaquely. A closing call
    with the same ``step_key`` and ``lifecycle_state=done`` is emitted at
    the end of the node.
    """
    capability = state.get("capability", "summarize")
    source_paths = state.get("validated_paths") or state.get("source_paths") or []
    count, source_kind = _count_and_kind_from_paths(capability, source_paths)
    step_key, title, summary_template, summary_facts = _execution_step_for_capability(
        capability, count
    )
    if capability != "analyze":
        summary_facts["source_kind"] = source_kind
    record_office_step(
        state,
        step_key=step_key,
        title=title,
        lifecycle_state=state.get("lifecycle_state", LIFECYCLE_RUNNING),
        summary_template=summary_template,
        summary_facts=summary_facts,
    )


def emit_summarizing(
    state: dict,
    *,
    lifecycle_state: str = LIFECYCLE_RUNNING,
) -> None:
    source_paths = state.get("validated_paths") or state.get("source_paths") or []
    source_count = len(source_paths) if isinstance(source_paths, list) else 0
    record_office_step(
        state,
        step_key="office.summarizing",
        title="Office summarizing each document",
        lifecycle_state=lifecycle_state,
        summary_template="Office summarized each of the {source_count} document(s).",
        summary_facts={"source_count": source_count},
    )


def emit_combining(
    state: dict,
    *,
    lifecycle_state: str = LIFECYCLE_RUNNING,
) -> None:
    source_paths = state.get("validated_paths") or state.get("source_paths") or []
    source_count = len(source_paths) if isinstance(source_paths, list) else 0
    record_office_step(
        state,
        step_key="office.combining",
        title="Office creating combined summary",
        lifecycle_state=lifecycle_state,
        summary_template="Office created the combined summary covering all {source_count} document(s).",
        summary_facts={"source_count": source_count},
        conditional=True,
    )


def emit_capability_completion_rows(state: dict) -> None:
    """Close proposal-aligned intermediate rows once the opaque core succeeds."""
    capability = state.get("capability", "summarize")
    source_paths = state.get("validated_paths") or state.get("source_paths") or []
    count = len(source_paths) if isinstance(source_paths, list) else 0

    if capability == "analyze":
        # Per design doc §3.3.2 the analyze skeleton uses ``{field_count}`` and
        # ``{numeric_field_count}`` placeholders. The agent's ReAct core
        # currently does not surface those numbers as structured state, so we
        # seed the templates with the available count and let the renderer
        # substitute the missing fields as ``--`` (per §6.3 fallback).
        record_office_step(
            state,
            step_key="office.inferring_schema",
            title="Office inferring data schema",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template=(
                "Office inferred the data schema: {field_count} field(s) "
                "detected across {source_count} file(s)."
            ),
            summary_facts={
                "source_count": count,
                "field_count": state.get("inferred_field_count", "unknown"),
            },
        )
        record_office_step(
            state,
            step_key="office.computing_stats",
            title="Office computing statistics",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template=(
                "Office computed summary statistics for {numeric_field_count} "
                "numeric field(s)."
            ),
            summary_facts={
                "numeric_field_count": state.get("numeric_field_count", "unknown"),
            },
        )
        record_office_step(
            state,
            step_key="office.generating_report",
            title="Office generating analysis report",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Office generated the analysis report from the inferred schema.",
        )
        return

    if capability == "organize":
        organize_file_count = int(state.get("organize_file_count") or count)
        record_office_step(
            state,
            step_key="office.scanning",
            title="Office scanning folder structure",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Office scanned the folder and inventoried {file_count} file(s).",
            summary_facts={
                "file_count": organize_file_count,
            },
        )
        record_office_step(
            state,
            step_key="office.planning",
            title="Office planning organization",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Office planned the organization around {grouping_criteria}.",
            summary_facts={
                "grouping_criteria": state.get(
                    "grouping_criteria", "discovered structural patterns"
                ),
            },
        )
        record_office_step(
            state,
            step_key="office.creating_folders",
            title="Office creating folder structure",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Office created the organized folder structure.",
        )
        return

    record_office_step(
        state,
        step_key="office.reading",
        title="Office reading documents",
        lifecycle_state=LIFECYCLE_DONE,
        summary_template="Office read {source_count} {source_kind} via MCP tools.",
        summary_facts={
            "source_count": count,
            "source_kind": _source_kind(capability, count),
        },
    )
    record_office_step(
        state,
        step_key="office.summarizing",
        title="Office summarizing each document",
        lifecycle_state=LIFECYCLE_DONE,
        summary_template="Office summarized each of the {source_count} document(s).",
        summary_facts={"source_count": count},
    )
    if count > 1:
        record_office_step(
            state,
            step_key="office.combining",
            title="Office creating combined summary",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Office created the combined summary covering all {source_count} document(s).",
            summary_facts={"source_count": count},
            conditional=True,
        )


def emit_writing(
    state: dict,
    *,
    output_count: int,
    file_count: int = 0,
    output_location: str | None = None,
    lifecycle_state: str = LIFECYCLE_RUNNING,
) -> None:
    """Emit ``office.writing`` (or, for organize, ``office.moving_files``)."""
    if output_location is None:
        output_location = _output_location_for_state(state)
    capability = state.get("capability", "summarize")
    step_key = "office.writing"
    title = "Office writing deliverable"
    summary_template = "Office wrote {output_count} deliverable(s) to {output_location}."
    summary_facts: dict[str, Any] = {
        "output_count": output_count,
        "output_location": output_location,
    }
    if capability == "analyze":
        summary_template = "Office wrote {output_count} analysis report(s) to {output_location}."
    elif capability == "summarize":
        summary_template = "Office wrote {output_count} summary file(s) to {output_location}."
    elif capability == "organize":
        step_key = "office.moving_files"
        title = "Office moving files into organized structure"
        summary_template = (
            "Office placed {file_count} file(s) into their organized locations under {output_location}."
        )
        summary_facts = {
            "file_count": file_count or output_count,
            "output_location": output_location,
        }
    record_office_step(
        state,
        step_key=step_key,
        title=title,
        lifecycle_state=lifecycle_state,
        summary_template=summary_template,
        summary_facts=summary_facts,
    )


def emit_writing_plan(state: dict, *, output_location: str | None = None) -> None:
    """Emit ``office.writing_plan`` for organize tasks."""
    if output_location is None:
        output_location = _output_location_for_state(state)
    record_office_step(
        state,
        step_key="office.writing_plan",
        title="Office writing organization plan",
        lifecycle_state=LIFECYCLE_DONE,
        summary_template="Office wrote the organization plan to {output_location}.",
        summary_facts={"output_location": output_location},
    )


def emit_verifying(
    state: dict,
    *,
    output_count: int,
    lifecycle_state: str = LIFECYCLE_DONE,
) -> None:
    """Emit ``office.verifying`` after delivery-path validation succeeds."""
    record_office_step(
        state,
        step_key="office.verifying",
        title="Office verifying deliverable",
        lifecycle_state=lifecycle_state,
        summary_template="Office verified {output_count} deliverable(s).",
        summary_facts={"output_count": output_count},
    )


def emit_delivered(
    state: dict,
    *,
    success: bool,
    output_count: int = 0,
    output_location: str | None = None,
) -> None:
    """Emit ``office.delivered`` to close the timeline."""
    if output_location is None:
        output_location = _output_location_for_state(state)
    lifecycle_state = LIFECYCLE_DONE if success else LIFECYCLE_FAILED
    record_office_step(
        state,
        step_key="office.delivered",
        title="Office delivering report to Compass",
        lifecycle_state=lifecycle_state,
        summary_template=(
            "Office delivered the report to Compass."
            if success
            else "Office could not deliver the report: {failure_reason}."
        ),
        summary_facts={
            "output_count": output_count,
            "output_location": output_location,
            "failure_reason": state.get("summary", "")[:200] if not success else "",
        },
    )


def emit_validating_plan_output(
    state: dict,
    *,
    lifecycle_state: str,
    summary_template: str,
    summary_facts: dict | None = None,
) -> None:
    """Emit the plan-output validation step.

    ``lifecycle_state`` must be one of: ``running``, ``done``, ``warning``.
    """
    record_office_step(
        state,
        step_key="office.validating_plan_output",
        title="Office validating output against plan",
        lifecycle_state=lifecycle_state,
        summary_template=summary_template,
        summary_facts=summary_facts,
    )


def emit_reconciling_plan_output(
    state: dict,
    *,
    lifecycle_state: str,
    round: int,
    summary_template: str,
    summary_facts: dict | None = None,
) -> None:
    """Emit a per-round reconciliation step.

    The round number becomes part of ``step_instance_key`` so the UI can
    show up to three reconciliation rows.
    """
    record_office_step(
        state,
        step_key="office.reconciling_plan_output",
        title="Office reconciling output to match plan",
        lifecycle_state=lifecycle_state,
        summary_template=summary_template,
        summary_facts=summary_facts,
        conditional=True,
        round=round,
    )


def emit_gate_exhausted(
    state: dict,
    *,
    round_count: int = 0,
    summary_facts: dict | None = None,
) -> None:
    """Emit the gate-exhaustion warning row.

    The default summary_template references ``{round_count}``; callers may
    pass additional ``summary_facts`` to enrich the rendered row (e.g. with
    missing_count, unexpected_count, no_progress_count).
    """
    facts = dict(summary_facts or {})
    facts.setdefault("round_count", round_count)
    record_office_step(
        state,
        step_key="office.gate_exhausted",
        title="Office plan-output gate exhausted",
        lifecycle_state=LIFECYCLE_WARNING,
        summary_template=(
            "Office could not fully reconcile the output with the declared plan after {round_count} round(s)."
        ),
        summary_facts=facts,
        conditional=True,
    )
