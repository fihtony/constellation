"""Common API for recording "major workflow step" events in the timeline.

This module is the single writer for the workflow timeline. Every agent must
call :func:`record_major_step` instead of mutating task metadata directly.
The UI (Compass) reads from these structured fields and renders them as the
"Workflow Timeline" panel.

Design references
-----------------
``docs/2026-06-02-workflow-timeline-redesign-zh.md`` §0 (unified contract) and
§10 (this module's spec).

The contract:

* ``step_instance_key = f"{step_key}#{round}"`` is the only row identity.
* ``lifecycle_state`` drives the row's true state; ``visual_state`` drives the
  UI glyph and is derived when not provided.
* Terminal rows (``done`` / ``failed`` / ``cancelled`` / ``terminated``) close
  the task: any later non-terminal event is appended to ``major_step_events``
  with ``ignored_after_terminal=true`` but does NOT create a new row.
* ``progress_sink`` is used to propagate events from a downstream agent's
  task store to the orchestrator's (Compass's) task store.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle states
# ---------------------------------------------------------------------------

LIFECYCLE_PENDING = "pending"
LIFECYCLE_CONDITIONAL_PENDING = "conditional_pending"
LIFECYCLE_RUNNING = "running"
LIFECYCLE_WAITING_FOR_USER = "waiting_for_user"
LIFECYCLE_RESUMING = "resuming"
LIFECYCLE_DONE = "done"
LIFECYCLE_WARNING = "warning"
LIFECYCLE_FAILED = "failed"
LIFECYCLE_CANCELLED = "cancelled"
LIFECYCLE_TERMINATED = "terminated"

LIFECYCLE_STATES: frozenset[str] = frozenset(
    {
        LIFECYCLE_PENDING,
        LIFECYCLE_CONDITIONAL_PENDING,
        LIFECYCLE_RUNNING,
        LIFECYCLE_WAITING_FOR_USER,
        LIFECYCLE_RESUMING,
        LIFECYCLE_DONE,
        LIFECYCLE_WARNING,
        LIFECYCLE_FAILED,
        LIFECYCLE_CANCELLED,
        LIFECYCLE_TERMINATED,
    }
)

TERMINAL_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {LIFECYCLE_DONE, LIFECYCLE_FAILED, LIFECYCLE_CANCELLED, LIFECYCLE_TERMINATED}
)


# ---------------------------------------------------------------------------
# Visual states
# ---------------------------------------------------------------------------

VISUAL_PENDING = "pending"
VISUAL_CONDITIONAL_PENDING = "conditional_pending"
VISUAL_CURRENT = "current"
VISUAL_WARN = "warn"
VISUAL_DONE = "done"
VISUAL_FAILED = "failed"

VISUAL_STATES: frozenset[str] = frozenset(
    {
        VISUAL_PENDING,
        VISUAL_CONDITIONAL_PENDING,
        VISUAL_CURRENT,
        VISUAL_WARN,
        VISUAL_DONE,
        VISUAL_FAILED,
    }
)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

_LIFECYCLE_TO_VISUAL: dict[str, str] = {
    LIFECYCLE_PENDING: VISUAL_PENDING,
    LIFECYCLE_CONDITIONAL_PENDING: VISUAL_CONDITIONAL_PENDING,
    LIFECYCLE_RUNNING: VISUAL_CURRENT,
    LIFECYCLE_WAITING_FOR_USER: VISUAL_WARN,
    LIFECYCLE_RESUMING: VISUAL_CURRENT,
    LIFECYCLE_DONE: VISUAL_DONE,
    LIFECYCLE_WARNING: VISUAL_WARN,
    LIFECYCLE_FAILED: VISUAL_FAILED,
    LIFECYCLE_CANCELLED: VISUAL_FAILED,
    LIFECYCLE_TERMINATED: VISUAL_FAILED,
}


def default_visual_state(lifecycle_state: str) -> str:
    """Return the default ``visual_state`` for a given ``lifecycle_state``."""
    return _LIFECYCLE_TO_VISUAL.get(lifecycle_state, VISUAL_PENDING)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_STEP_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_SECRET_KEY_RE = re.compile(r"^secret[_-]")
_SENSITIVE_VALUE_RE = re.compile(
    r"(ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
    r"ATATT[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9_\-\.]{16,})"
)


def _now_iso() -> str:
    """UTC ISO-8601 timestamp with explicit offset (matches ``devlog``)."""
    return datetime.now(timezone.utc).isoformat()


def _validate_step_key(step_key: str) -> None:
    if not isinstance(step_key, str) or not step_key:
        raise ValueError("step_key must be a non-empty string")
    if not _STEP_KEY_RE.match(step_key):
        raise ValueError(
            f"step_key {step_key!r} must match {_STEP_KEY_RE.pattern} "
            "(lowercase, dotted, English identifier)"
        )


def _validate_summary_facts(facts: dict | None) -> dict:
    if facts is None:
        return {}
    if not isinstance(facts, dict):
        raise ValueError("summary_facts must be a dict or None")
    for key, value in facts.items():
        if _SECRET_KEY_RE.match(str(key)):
            raise ValueError(
                f"summary_facts key {key!r} is reserved for secrets and cannot be persisted"
            )
        rendered = str(value)
        if _SENSITIVE_VALUE_RE.search(rendered):
            raise ValueError(
                f"summary_facts[{key!r}] looks like a sensitive token; refusing to persist"
            )
    try:
        json.dumps(facts, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"summary_facts must be JSON-serializable: {exc}") from exc
    return facts


# ---------------------------------------------------------------------------
# MajorStepSink — fan-out of events to a destination
# ---------------------------------------------------------------------------

class MajorStepSink(ABC):
    """Abstract destination for major step events.

    The default implementation writes to a :class:`TaskStore`. Cross-process
    sinks (HTTP) propagate to the orchestrator's task store.
    """

    @abstractmethod
    def handle_event(self, event: dict) -> None:
        """Persist or forward a single major step event."""


class NullMajorStepSink(MajorStepSink):
    """No-op sink for test isolation or when the registry is unavailable."""

    def handle_event(self, event: dict) -> None:
        return None


class InProcessMajorStepSink(MajorStepSink):
    """Write events to a local ``TaskStore`` (same process as the agent)."""

    def __init__(self, task_store: Any) -> None:
        self._task_store = task_store

    def handle_event(self, event: dict) -> None:
        # Use the public API so the existing per-instance lock covers atomicity.
        from framework.major_step import record_major_step  # avoid cycle
        try:
            record_major_step(
                event["task_id"],
                step_key=event["step_key"],
                title=event.get("title", ""),
                agent=event.get("agent", ""),
                lifecycle_state=event.get("lifecycle_state", "running"),
                visual_state=event.get("visual_state"),
                summary_template=event.get("summary_template", ""),
                summary_facts=event.get("summary_facts"),
                round=int(event.get("round", 0)),
                conditional=bool(event.get("conditional", False)),
                task_store=self._task_store,
            )
        except Exception as exc:  # noqa: BLE001 - sink never raises
            logger.debug("[major-step-sink] in-process write failed: %s", exc)


class HttpMajorStepSink(MajorStepSink):
    """Fire-and-forget HTTP POST to the orchestrator's sink endpoint.

    Network errors are logged and swallowed. The sender's own task store still
    gets the row via the local :class:`InProcessMajorStepSink`, so debugging on
    the sender side is unaffected.
    """

    def __init__(self, callback_url: str, timeout_seconds: float = 0.2) -> None:
        self._callback_url = callback_url.rstrip("/") if callback_url else ""
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._last_post_ts: float = 0.0

    def handle_event(self, event: dict) -> None:
        if not self._callback_url:
            return
        # Run the POST in a daemon thread so callers never block.
        thread = threading.Thread(
            target=self._post,
            args=(event,),
            daemon=True,
            name="major-step-http-sink",
        )
        thread.start()

    def _post(self, event: dict) -> None:
        import urllib.request
        try:
            payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                self._callback_url,
                data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with self._lock:
                # 50ms minimum spacing per worker to avoid hot loops.
                elapsed = time.time() - self._last_post_ts
                if elapsed < 0.05:
                    time.sleep(0.05 - elapsed)
                self._last_post_ts = time.time()
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                # Drain to allow connection reuse; status is best-effort.
                resp.read(1)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[major-step-sink] HTTP POST to %s failed: %s", self._callback_url, exc
            )


# ---------------------------------------------------------------------------
# Sink resolution — must go through Capability Registry
# ---------------------------------------------------------------------------

DEFAULT_SINK_CAPABILITY = "compass.major_step.sink"
DEFAULT_SINK_ENV_VAR = "MAJOR_STEP_SINK_URL"
DEFAULT_SINK_PATH = "/_major_step/events"


def resolve_progress_sink(
    orchestrator_task_id: str = "",
    *,
    capability_id: str = DEFAULT_SINK_CAPABILITY,
    env_var: str = DEFAULT_SINK_ENV_VAR,
    path: str = DEFAULT_SINK_PATH,
    task_store: Any | None = None,
) -> MajorStepSink:
    """Resolve a :class:`MajorStepSink` for an orchestrator task.

    Resolution order (per CLAUDE.md "resolve through Capability Registry first"):

    1. ``env_var`` env var override (intended only for local development).
    2. :class:`RegistryClient` lookup of ``capability_id`` (the
       ``compass.major_step.sink`` capability). Appends ``path`` to the
       service URL.
    3. If ``task_store`` is supplied, return an :class:`InProcessMajorStepSink`
       bound to it (covers Compass self-calls and in-process test setups).
    4. :class:`NullMajorStepSink` — events are dropped with a warning log.
    """
    env_url = os.environ.get(env_var, "").strip()
    if env_url:
        return HttpMajorStepSink(env_url)

    try:
        from framework.registry_client import RegistryClient

        registry = RegistryClient.from_config()
        service_url = registry.discover(capability_id)
        if service_url:
            return HttpMajorStepSink(f"{service_url.rstrip('/')}{path}")
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[major-step] registry lookup for %s failed: %s", capability_id, exc
        )

    if task_store is not None:
        return InProcessMajorStepSink(task_store)

    logger.debug(
        "[major-step] no progress_sink resolved for task_id=%s; using NullMajorStepSink",
        orchestrator_task_id or "<none>",
    )
    return NullMajorStepSink()


# ---------------------------------------------------------------------------
# record_major_step — the single writer
# ---------------------------------------------------------------------------

def _project_to_legacy_progress_steps(
    events: list[dict], existing_progress_steps: list[dict]
) -> list[dict]:
    """Derive the legacy ``progress_steps`` view from ``major_step_events``.

    Kept for backward compat with the pre-v0.8 UI. Each emitted row contributes
    a ``{"text", "agent", "ts"}`` entry. Updates to the same
    ``step_instance_key`` (later lifecycle changes) replace the prior text
    with the latest title.
    """
    if not events:
        return list(existing_progress_steps or [])
    by_key: dict[str, dict] = {}
    for ev in events:
        if ev.get("ignored_after_terminal"):
            continue
        sik = ev.get("step_instance_key")
        if not sik:
            continue
        by_key[sik] = {
            "text": ev.get("title", ""),
            "agent": ev.get("agent", ""),
            "ts": ev.get("created_at", ""),
        }
    return list(by_key.values())


def record_major_step(
    task_id: str,
    *,
    step_key: str,
    title: str,
    agent: str,
    lifecycle_state: str = LIFECYCLE_RUNNING,
    visual_state: str | None = None,
    summary_template: str = "",
    summary_facts: dict | None = None,
    round: int = 0,
    conditional: bool = False,
    orchestrator_task_id: str = "",
    progress_sink: MajorStepSink | None = None,
    task_store: Any | None = None,
) -> dict:
    """Record a major workflow step event and update the corresponding UI row.

    Parameters
    ----------
    task_id:
        The task to update. For an agent that shares the orchestrator's
        ``TaskStore``, this is the same id. For an agent with an isolated
        ``TaskStore``, this is the agent's own task id and
        ``orchestrator_task_id`` identifies the orchestrator (Compass) task.
    step_key, title, agent:
        Required. ``step_key`` must match ``^[a-z][a-z0-9_]*\\.[a-z][a-z0-9_]*$``.
    lifecycle_state:
        One of :data:`LIFECYCLE_STATES`. Drives the row's true state.
    visual_state:
        Optional override; defaults to the lifecycle→visual mapping.
    summary_template, summary_facts:
        Template skeleton (English) and runtime substitution facts.
    round:
        Loop iteration number; ``0`` for first occurrence.
    conditional:
        Whether the step is conditional (e.g. only fires if user picks
        ``inplace``). Skipped in skeleton rendering when unfired.
    orchestrator_task_id:
        Compass's top-level task id. If set and differs from ``task_id``, the
        event is also forwarded to ``progress_sink`` for fan-out.
    progress_sink:
        A :class:`MajorStepSink` to receive the event. If ``None`` and
        ``orchestrator_task_id`` is set, :func:`resolve_progress_sink` is
        called lazily.
    task_store:
        The :class:`TaskStore` to write to. Required for in-process writes.

    Returns
    -------
    dict
        The event that was appended to ``major_step_events``.
    """
    # ---- validation -------------------------------------------------------
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id must be a non-empty string")
    if not isinstance(title, str) or not title:
        raise ValueError("title must be a non-empty string")
    if not isinstance(agent, str) or not agent:
        raise ValueError("agent must be a non-empty string")
    _validate_step_key(step_key)
    if lifecycle_state not in LIFECYCLE_STATES:
        raise ValueError(
            f"lifecycle_state {lifecycle_state!r} is not one of {sorted(LIFECYCLE_STATES)}"
        )
    if visual_state is not None and visual_state not in VISUAL_STATES:
        raise ValueError(
            f"visual_state {visual_state!r} is not one of {sorted(VISUAL_STATES)}"
        )
    if not isinstance(round, int) or round < 0:
        raise ValueError("round must be an integer >= 0")
    facts = _validate_summary_facts(summary_facts)

    step_instance_key = f"{step_key}#{round}"
    resolved_visual = visual_state or default_visual_state(lifecycle_state)
    now = _now_iso()
    event = {
        "event_id": f"evt-{uuid.uuid4().hex[:12]}",
        "task_id": task_id,
        "orchestrator_task_id": orchestrator_task_id or task_id,
        "step_key": step_key,
        "step_instance_key": step_instance_key,
        "round": round,
        "title": title,
        "agent": agent,
        "lifecycle_state": lifecycle_state,
        "visual_state": resolved_visual,
        "summary_template": summary_template or "",
        "summary_facts": facts,
        "conditional": bool(conditional),
        "created_at": now,
    }

    # ---- write to local task store ---------------------------------------
    if task_store is not None:
        _merge_into_task_store(task_store, task_id, event, lifecycle_state)

    # ---- fan-out to orchestrator -----------------------------------------
    # Always propagate when an orchestrator_task_id is supplied, even if it
    # equals ``task_id``. In cross-process deployments (e.g. per-task Office
    # container with its own TaskStore), the orchestrator's TaskStore is a
    # different physical store, so we must push the event over the network.
    # The HTTP receiver is idempotent on ``(step_key, round)`` so duplicate
    # writes are safe.
    if orchestrator_task_id:
        if progress_sink is None:
            progress_sink = resolve_progress_sink(
                orchestrator_task_id, task_store=task_store
            )
        try:
            progress_sink.handle_event(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[major-step] progress_sink.handle_event failed: %s", exc
            )

    return event


def ensure_major_step_skeleton(
    task_id: str,
    *,
    entries: list[dict] | tuple[dict, ...],
    task_store: Any | None = None,
) -> list[dict]:
    """Ensure ``major_step_skeleton`` contains the provided ordered rows.

    Unlike :func:`record_major_step`, this helper does not create events or
    rows. It only seeds the render-order skeleton so Compass can show future
    pending steps before downstream agents emit them.
    """
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id must be a non-empty string")
    if task_store is None:
        raise ValueError("task_store is required to seed major_step_skeleton")
    if not isinstance(entries, (list, tuple)):
        raise ValueError("entries must be a list or tuple of skeleton rows")

    task = task_store.get_task(task_id)
    if task is None:
        return []

    metadata = dict(task.metadata or {})
    skeleton_raw = metadata.get("major_step_skeleton") or []
    if isinstance(skeleton_raw, dict):
        skeleton_list = list(skeleton_raw.values())
    else:
        skeleton_list = list(skeleton_raw)
    skeleton_keys = {
        row.get("step_instance_key")
        for row in skeleton_list
        if isinstance(row, dict) and row.get("step_instance_key")
    }

    added: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each skeleton entry must be a dict")
        step_key = str(entry.get("step_key") or "").strip()
        title = str(entry.get("title") or "").strip()
        agent = str(entry.get("agent") or "").strip()
        round_value = int(entry.get("round", 0))
        conditional = bool(entry.get("conditional", False))
        if not title:
            raise ValueError("skeleton entry title must be a non-empty string")
        if not agent:
            raise ValueError("skeleton entry agent must be a non-empty string")
        _validate_step_key(step_key)
        if round_value < 0:
            raise ValueError("skeleton entry round must be >= 0")

        step_instance_key = str(entry.get("step_instance_key") or "").strip()
        if not step_instance_key:
            step_instance_key = f"{step_key}#{round_value}"
        if step_instance_key in skeleton_keys:
            continue

        row = {
            "step_key": step_key,
            "step_instance_key": step_instance_key,
            "round": round_value,
            "title": title,
            "agent": agent,
            "conditional": conditional,
        }
        skeleton_list.append(row)
        skeleton_keys.add(step_instance_key)
        added.append(row)

    if added:
        task_store.update_metadata(task_id, {"major_step_skeleton": skeleton_list})
    return added


# ---------------------------------------------------------------------------
# Internal merge logic
# ---------------------------------------------------------------------------

def _merge_into_task_store(
    task_store: Any,
    task_id: str,
    event: dict,
    lifecycle_state: str,
) -> None:
    """Append the event and upsert the corresponding row + pointer fields.

    All writes are funneled through ``task_store.update_metadata`` so the
    existing per-instance ``threading.Lock`` covers atomicity.
    """
    task = task_store.get_task(task_id)
    if task is None:
        return
    metadata = dict(task.metadata or {})

    major_step_events = list(metadata.get("major_step_events") or [])
    major_step_rows = dict(metadata.get("major_step_rows") or {})
    # ``major_step_skeleton`` is stored as a list (per design doc §6.1) so the
    # UI can iterate it in insertion order. Idempotence is enforced by deduping
    # on ``step_instance_key`` before appending.
    skeleton_raw = metadata.get("major_step_skeleton") or []
    if isinstance(skeleton_raw, dict):
        skeleton_list = list(skeleton_raw.values())
    else:
        skeleton_list = list(skeleton_raw)
    skeleton_keys = {row.get("step_instance_key") for row in skeleton_list if row.get("step_instance_key")}
    step_states = dict(metadata.get("step_states") or {})
    step_summaries = dict(metadata.get("step_summaries") or {})

    sik = event["step_instance_key"]
    is_terminal_event = lifecycle_state in TERMINAL_LIFECYCLE_STATES
    existing_row = major_step_rows.get(sik)
    previous_row_terminal = bool(
        existing_row
        and existing_row.get("lifecycle_state") in TERMINAL_LIFECYCLE_STATES
    )
    active_key = metadata.get("active_step_instance_key", "")
    last_key = metadata.get("last_step_instance_key", "")
    failed_key = metadata.get("failed_step_instance_key", "")
    terminal_key = metadata.get("terminal_step_instance_key", "")
    # Task-wide terminal protection only applies after the workflow has
    # emitted a canonical terminal pointer (for example
    # ``compass.task_completed#0``). Ordinary completed intermediate rows like
    # ``compass.dispatched#0`` or ``office.reading#0`` must not freeze the
    # timeline; otherwise downstream running steps can only appear after the
    # task is already complete.
    is_task_terminal = bool(terminal_key)

    if is_task_terminal and not is_terminal_event:
        # Terminal protection: append event with ignored_after_terminal=true
        # but do NOT touch major_step_rows.
        event_for_log = dict(event)
        event_for_log["ignored_after_terminal"] = True
        major_step_events.append(event_for_log)
        task_store.update_metadata(
            task_id,
            {
                "major_step_events": major_step_events,
            },
        )
        return

    if previous_row_terminal and not is_terminal_event:
        # Per-row terminal protection: late duplicate updates for the same
        # step_instance_key must not reopen a row that already reached a
        # terminal lifecycle (for example a duplicate ``resuming`` event
        # arriving after ``compass.asking_output_mode#0`` was already ``done``).
        event_for_log = dict(event)
        event_for_log["ignored_after_terminal"] = True
        major_step_events.append(event_for_log)
        task_store.update_metadata(
            task_id,
            {
                "major_step_events": major_step_events,
            },
        )
        return

    major_step_events.append(event)

    # Single-active-row normalization: when a new step instance starts (or the
    # task emits a different terminal row), any previously-active non-terminal
    # row must be closed so its visual state and elapsed time stop advancing.
    previous_active_key = active_key if active_key and active_key != sik else ""
    if previous_active_key:
        previous_active_row = major_step_rows.get(previous_active_key)
        previous_active_state = step_states.get(previous_active_key) or {}
        previous_active_terminal = bool(
            previous_active_row
            and previous_active_row.get("lifecycle_state") in TERMINAL_LIFECYCLE_STATES
        )
        if previous_active_row and not previous_active_terminal:
            closed_row = dict(previous_active_row)
            closed_row["lifecycle_state"] = LIFECYCLE_DONE
            closed_row["visual_state"] = VISUAL_DONE
            closed_row["ended_at"] = event["created_at"]
            major_step_rows[previous_active_key] = closed_row

            closed_state = dict(previous_active_state)
            closed_state["lifecycle_state"] = LIFECYCLE_DONE
            closed_state["visual_state"] = VISUAL_DONE
            if "started_at" not in closed_state and closed_row.get("started_at"):
                closed_state["started_at"] = closed_row.get("started_at")
            closed_state["ended_at"] = event["created_at"]
            step_states[previous_active_key] = closed_state

    # Upsert row in major_step_rows
    previous_row = existing_row
    new_row = dict(previous_row or {})
    new_row.update(
        {
            "step_key": event["step_key"],
            "step_instance_key": sik,
            "round": event["round"],
            "title": event["title"],
            "agent": event["agent"],
            "lifecycle_state": lifecycle_state,
            "visual_state": event["visual_state"],
            "summary_template": event["summary_template"],
            "summary_facts": event["summary_facts"],
            "conditional": event["conditional"],
        }
    )
    if "started_at" not in new_row:
        new_row["started_at"] = event["created_at"]
    if is_terminal_event:
        new_row["ended_at"] = event["created_at"]
    elif lifecycle_state == LIFECYCLE_WAITING_FOR_USER:
        # Waiting: keep started_at; do NOT set ended_at yet.
        new_row["ended_at"] = None
    elif lifecycle_state == LIFECYCLE_RESUMING:
        # Resume: keep prior ended_at (None while waiting).
        new_row["ended_at"] = previous_row.get("ended_at") if previous_row else None
    new_row["ignored_after_terminal"] = False
    major_step_rows[sik] = new_row

    # Upsert skeleton (list with dedup on step_instance_key).
    if sik not in skeleton_keys:
        skeleton_list.append(
            {
                "step_key": event["step_key"],
                "step_instance_key": sik,
                "round": event["round"],
                "title": event["title"],
                "agent": event["agent"],
                "conditional": event["conditional"],
            }
        )
        skeleton_keys.add(sik)

    # Upsert step_states
    state_entry = dict(step_states.get(sik) or {})
    state_entry.update(
        {
            "lifecycle_state": lifecycle_state,
            "visual_state": event["visual_state"],
        }
    )
    if "started_at" not in state_entry:
        state_entry["started_at"] = event["created_at"]
    if is_terminal_event:
        state_entry["ended_at"] = event["created_at"]
    elif lifecycle_state == LIFECYCLE_WAITING_FOR_USER:
        state_entry["ended_at"] = None
    elif lifecycle_state == LIFECYCLE_RESUMING:
        state_entry["ended_at"] = state_entry.get("ended_at")
    step_states[sik] = state_entry

    # Upsert step_summaries
    step_summaries[sik] = {
        "summary_template": event["summary_template"],
        "summary_facts": event["summary_facts"],
    }

    # Update pointer fields per §0.6.
    if is_terminal_event and sik.startswith("compass.task_"):
        # Always-fires Compass terminal rows become the canonical terminal pointer.
        terminal_key = sik

    if is_terminal_event and lifecycle_state in (
        LIFECYCLE_FAILED,
        LIFECYCLE_CANCELLED,
        LIFECYCLE_TERMINATED,
    ):
        # First/specific failed step becomes the failed pointer.
        if not failed_key:
            failed_key = sik

    # Active pointer: clear for terminal events; otherwise the latest non-terminal
    # step is active. For waiting_for_user and resuming, the row stays active.
    if is_terminal_event and sik == terminal_key:
        # Compass terminal row: the task is closed; clear active pointer.
        active_key = ""
    elif is_terminal_event:
        # In-flight step that closed (e.g., wd.implementing→cancelled): clear active.
        if active_key == sik:
            active_key = ""
    elif lifecycle_state in (LIFECYCLE_RUNNING, LIFECYCLE_WAITING_FOR_USER, LIFECYCLE_RESUMING):
        active_key = sik

    last_key = sik

    # Derive legacy progress_steps for backward compat.
    progress_steps = _project_to_legacy_progress_steps(major_step_events, metadata.get("progress_steps") or [])
    current_major_step = ""
    if sik in major_step_rows:
        current_major_step = major_step_rows[sik].get("title", "")

    update_delta = {
        "major_step_events": major_step_events,
        "major_step_rows": major_step_rows,
        "major_step_skeleton": skeleton_list,
        "step_states": step_states,
        "step_summaries": step_summaries,
        "active_step_instance_key": active_key,
        "last_step_instance_key": last_key,
        "failed_step_instance_key": failed_key,
        "terminal_step_instance_key": terminal_key,
        # Legacy fields, kept for pre-v0.8 UI.
        "progress_steps": progress_steps,
        "current_major_step": current_major_step,
    }
    task_store.update_metadata(task_id, update_delta)
