"""Per-task agent lifecycle manager.

Implements the lifecycle state machine, idle timeout, ACK-triggered shutdown,
keep-alive ping handling, and terminate support for per-task agents.

Exit codes:
  0 — Graceful exit after parent ACK (task_completed_success)
  1 — Forced termination via /terminate or unrecoverable error
  2 — Idle timeout (no new work within timeout period)

State machine:
  SUBMITTED_WAITING → WORKING → IDLE_WAITING → (exit)
                                       ↑
                          (new task) ───┘

See docs/code-review-workflow-requirements-zh.md § 17 for full specification.
"""
from __future__ import annotations

import json
import os
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional
from urllib.request import Request, urlopen


class LifecycleState(str, Enum):
    """Per-task agent lifecycle states."""

    SUBMITTED_WAITING = "SUBMITTED_WAITING"
    WORKING = "WORKING"
    IDLE_WAITING = "IDLE_WAITING"
    SHUTTING_DOWN = "SHUTTING_DOWN"


# Exit codes
EXIT_ACK = 0  # Parent acknowledged, graceful shutdown
EXIT_TERMINATE = 1  # Forced termination
EXIT_IDLE_TIMEOUT = 2  # No work within timeout


class PerTaskLifecycleManager:
    """Manages per-task agent lifecycle: idle timeout, ACK, ping, terminate.

    Usage:
        lifecycle = PerTaskLifecycleManager(
            agent_id="web-dev",
            idle_timeout_seconds=1800,
            on_timeout_notify=my_notify_fn,  # optional: best-effort parent notify
        )
        lifecycle.mark_working()         # when task starts
        lifecycle.arm_idle_timer()       # when task completes, waiting for next
        lifecycle.cancel_idle_timer()    # when new task arrives
        lifecycle.handle_ack(task_id)    # called by A2A server on /tasks/{id}/ack
        lifecycle.handle_ping(task_id)   # called by A2A server on /tasks/{id}/ping
        lifecycle.handle_terminate(task_id)  # called by A2A server on /tasks/{id}/terminate
    """

    def __init__(
        self,
        agent_id: str,
        idle_timeout_seconds: float = 1800,
        on_timeout_notify: Optional[Callable[[str], None]] = None,
        workspace_path: str = "",
    ) -> None:
        self._agent_id = agent_id
        self._idle_timeout = idle_timeout_seconds
        self._on_timeout_notify = on_timeout_notify
        self._workspace_path = workspace_path

        self._state = LifecycleState.SUBMITTED_WAITING
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._current_task_id: str = ""
        self._acked_task_ids: set[str] = set()
        self._start_time = time.time()
        self._timeout_callback_url: str = ""
        self._timeout_orchestrator_task_id: str = ""
        self._registry_status_updater: Optional[Callable[..., None]] = None

    @property
    def state(self) -> LifecycleState:
        """Current lifecycle state."""
        return self._state

    @property
    def current_task_id(self) -> str:
        """The task ID currently being processed or last completed."""
        return self._current_task_id

    def configure_registry_updater(self, updater: Callable[..., None]) -> None:
        """Attach a best-effort registry status updater."""
        with self._lock:
            self._registry_status_updater = updater

    def mark_working(self, task_id: str = "") -> None:
        """Transition to WORKING state. Cancel any pending idle timer."""
        with self._lock:
            self._state = LifecycleState.WORKING
            if task_id:
                self._current_task_id = task_id
            self._cancel_timer_locked()
            current_task_id = self._current_task_id
        self._update_registry_status(status="busy", current_task_id=current_task_id)

    def arm_idle_timer(self, task_id: str = "") -> None:
        """Arm the idle timer after task completion. Transition to IDLE_WAITING."""
        with self._lock:
            self._state = LifecycleState.IDLE_WAITING
            if task_id:
                self._current_task_id = task_id
            self._cancel_timer_locked()
            self._timer = threading.Timer(self._idle_timeout, self._on_idle_timeout)
            self._timer.daemon = True
            self._timer.start()
            current_task_id = self._current_task_id
        self._update_registry_status(status="idle", current_task_id=current_task_id)

    def cancel_idle_timer(self) -> None:
        """Cancel the idle timer (call when a new task arrives)."""
        with self._lock:
            self._cancel_timer_locked()

    def configure_timeout_notification(
        self,
        callback_url: str,
        *,
        orchestrator_task_id: str = "",
    ) -> None:
        """Configure the best-effort parent notification endpoint for idle timeout."""
        with self._lock:
            self._timeout_callback_url = str(callback_url or "").strip()
            self._timeout_orchestrator_task_id = str(orchestrator_task_id or "").strip()

    def handle_ack(self, task_id: str) -> dict:
        """Handle ACK from parent. Triggers graceful shutdown.

        Returns dict with status info for the HTTP response.
        """
        with self._lock:
            if task_id in self._acked_task_ids:
                # Idempotent: already ACKed
                return {"status": "ok", "message": "already acknowledged"}
            self._acked_task_ids.add(task_id)
            self._state = LifecycleState.SHUTTING_DOWN
            self._cancel_timer_locked()

        self._update_registry_status(status="exited", current_task_id=None)
        self._audit_log("ACK_RECEIVED", task_id=task_id, exit_code=EXIT_ACK)
        self._schedule_exit(EXIT_ACK, reason=f"ACK received for task {task_id}")
        return {"status": "ok", "message": "shutting down"}

    def handle_ping(self, task_id: str) -> dict:
        """Handle keep-alive ping from parent. Resets idle timer if in IDLE_WAITING."""
        with self._lock:
            if self._state == LifecycleState.IDLE_WAITING:
                self._cancel_timer_locked()
                self._timer = threading.Timer(self._idle_timeout, self._on_idle_timeout)
                self._timer.daemon = True
                self._timer.start()
                current_task_id = self._current_task_id or task_id
                self._current_task_id = current_task_id
                should_update = True
            else:
                should_update = False
                current_task_id = self._current_task_id or task_id
        if should_update:
            self._update_registry_status(status="idle", current_task_id=current_task_id)
            return {"status": "ok", "message": "idle timer reset"}
        return {"status": "ok", "message": f"state={self._state.value}, no timer reset"}

    def handle_terminate(self, task_id: str) -> dict:
        """Handle forced termination request. Immediate shutdown with exit code 1."""
        with self._lock:
            self._state = LifecycleState.SHUTTING_DOWN
            self._cancel_timer_locked()

        self._update_registry_status(status="exited", current_task_id=None)
        self._audit_log("TERMINATE_RECEIVED", task_id=task_id, exit_code=EXIT_TERMINATE)
        self._schedule_exit(EXIT_TERMINATE, reason=f"Terminate requested for task {task_id}")
        return {"status": "ok", "message": "terminating"}

    def _cancel_timer_locked(self) -> None:
        """Cancel the timer (must hold self._lock)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_idle_timeout(self) -> None:
        """Called when idle timeout expires."""
        with self._lock:
            self._state = LifecycleState.SHUTTING_DOWN

        task_id = self._current_task_id
        self._update_registry_status(status="exited", current_task_id=None)
        self._audit_log(
            "IDLE_TIMEOUT",
            task_id=task_id,
            exit_code=EXIT_IDLE_TIMEOUT,
            timeout_seconds=self._idle_timeout,
        )

        # Best-effort notify parent of timeout
        if self._on_timeout_notify:
            try:
                self._on_timeout_notify(task_id)
            except Exception:
                pass
        self._notify_parent_timeout(task_id)

        print(
            f"[{self._agent_id}] Idle timeout ({int(self._idle_timeout)}s) reached "
            "— shutting down container (exit code 2)."
        )
        self._schedule_exit(EXIT_IDLE_TIMEOUT, reason="idle timeout")

    def _schedule_exit(self, exit_code: int, reason: str = "") -> None:
        """Schedule process exit after a brief delay for response delivery."""
        print(f"[{self._agent_id}] Scheduling exit (code={exit_code}): {reason}")

        def _do_exit():
            time.sleep(0.5)  # Allow HTTP response to flush
            os._exit(exit_code)

        t = threading.Thread(target=_do_exit, daemon=True)
        t.start()

    def _notify_parent_timeout(self, task_id: str) -> None:
        callback_url = ""
        orchestrator_task_id = ""
        with self._lock:
            callback_url = self._timeout_callback_url
            orchestrator_task_id = self._timeout_orchestrator_task_id

        if not callback_url or "/callbacks" not in callback_url:
            return

        timeout_url = callback_url.rsplit("/callbacks", 1)[0] + "/child-timeout"
        payload = {
            "childTaskId": task_id,
            "childAgentId": self._agent_id,
            "exitCode": EXIT_IDLE_TIMEOUT,
            "reason": "idle_timeout",
            "orchestratorTaskId": orchestrator_task_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            timeout_url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5):
                pass
        except Exception:
            pass

    def _audit_log(self, event: str, **kwargs: Any) -> None:
        """Write structured audit log entry."""
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "agent_id": self._agent_id,
            "event": event,
            "state": self._state.value,
            "uptime_seconds": int(time.time() - self._start_time),
            **kwargs,
        }
        print(f"[{self._agent_id}][lifecycle] {json.dumps(entry, ensure_ascii=False)}")

        # Write to workspace audit log if available
        workspace = self._workspace_path
        if workspace:
            try:
                agent_dir = os.path.join(workspace, self._agent_id)
                os.makedirs(agent_dir, exist_ok=True)
                audit_path = os.path.join(agent_dir, "lifecycle-audit.jsonl")
                with open(audit_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def _update_registry_status(self, **fields: Any) -> None:
        updater = None
        with self._lock:
            updater = self._registry_status_updater
        if updater is None:
            return
        try:
            updater(**fields)
        except Exception:
            pass
