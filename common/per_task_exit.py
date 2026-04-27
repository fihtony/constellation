"""Per-task agent exit rule support.

Per-task agents (executionMode=per-task) should wait for the parent agent to
confirm they are no longer needed before shutting down.  This avoids race
conditions where a parent tries to contact an agent that has already stopped,
and makes the lifecycle deterministic and observable.

Exit Rule Format (passed via message metadata field "exitRule"):
    {
        "type": "wait_for_parent_ack",   # default for per-task agents
        "ack_timeout_seconds": 300        # 5-minute default timeout
    }

Supported types:
    "wait_for_parent_ack"  — Block shutdown until parent calls
                             POST /tasks/{task_id}/ack, or until
                             ack_timeout_seconds elapses (then shut down anyway).
    "immediate"            — Shut down immediately after task completion
                             (old AUTO_STOP_AFTER_TASK=1 behaviour).
    "persistent"           — Never auto-stop (default for always-on agents).

Parent-agent responsibility:
    When the parent is done with the child (all revision cycles complete,
    callback processed, etc.), it sends:
        POST {child_service_url}/tasks/{child_task_id}/ack

If no ACK arrives within ack_timeout_seconds, the agent shuts down anyway
so stale containers do not accumulate.
"""

from __future__ import annotations

import threading

DEFAULT_ACK_TIMEOUT_SECONDS = 300  # 5 minutes


class PerTaskExitHandler:
    """Thread-safe per-task ACK registry for per-task agent exit rules.

    Typical usage (inside an agent):

        exit_handler = PerTaskExitHandler()   # module-level singleton

        # In _run_workflow:
        exit_rule = PerTaskExitHandler.parse(metadata)
        try:
            ...
            _notify_callback(...)
        finally:
            exit_handler.apply(task_id, exit_rule, shutdown_fn=_schedule_shutdown)

        # In HTTP handler (POST /tasks/{id}/ack):
        exit_handler.acknowledge(task_id)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ack_events: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------
    # Registration / acknowledgement
    # ------------------------------------------------------------------

    def register(self, task_id: str) -> threading.Event:
        """Register *task_id* as waiting for a parent ACK.

        Safe to call before the callback is sent (the event will already be
        set if ``acknowledge`` is called first — no race condition).
        """
        event = threading.Event()
        with self._lock:
            self._ack_events[task_id] = event
        return event

    def acknowledge(self, task_id: str) -> bool:
        """Signal that the parent has acknowledged *task_id*.

        Returns True if the task was registered and waiting; False if the
        task_id is unknown (possibly already cleaned up or timed out).
        """
        with self._lock:
            event = self._ack_events.get(task_id)
        if event:
            event.set()
            return True
        return False

    def wait(self, task_id: str, timeout: int = DEFAULT_ACK_TIMEOUT_SECONDS) -> bool:
        """Block until parent ACK is received or *timeout* seconds elapse.

        Returns True if the ACK was received, False on timeout.
        Removes the event from the registry regardless of outcome.
        """
        with self._lock:
            event = self._ack_events.get(task_id)
        if event is None:
            return False
        acked = event.wait(timeout=timeout)
        with self._lock:
            self._ack_events.pop(task_id, None)
        return acked

    def cleanup(self, task_id: str) -> None:
        """Remove *task_id* from the registry without waiting (for error paths)."""
        with self._lock:
            self._ack_events.pop(task_id, None)

    # ------------------------------------------------------------------
    # High-level helper
    # ------------------------------------------------------------------

    def apply(
        self,
        task_id: str,
        exit_rule: dict,
        *,
        shutdown_fn,
        agent_id: str = "agent",
    ) -> None:
        """Apply the exit rule for *task_id* after task completion.

        Blocks until the rule is satisfied, then calls ``shutdown_fn(delay_seconds=2)``.

        Args:
            task_id:     The task that just completed.
            exit_rule:   Dict returned by :meth:`parse`.
            shutdown_fn: Callable that accepts ``delay_seconds`` kwarg and
                         schedules the process shutdown (non-blocking).
            agent_id:    Agent name used in log messages.
        """
        rule_type = (exit_rule or {}).get("type", "wait_for_parent_ack")
        timeout = int((exit_rule or {}).get("ack_timeout_seconds", DEFAULT_ACK_TIMEOUT_SECONDS))

        if rule_type == "wait_for_parent_ack":
            # Register first so we don't miss an early ACK
            self.register(task_id)
            print(
                f"[{agent_id}] Waiting for parent ACK (task={task_id}, timeout={timeout}s)"
            )
            acked = self.wait(task_id, timeout=timeout)
            if acked:
                print(f"[{agent_id}] Parent ACK received for task {task_id} — shutting down")
            else:
                print(
                    f"[{agent_id}] Parent ACK timeout ({timeout}s) for task {task_id} — shutting down"
                )
            shutdown_fn(delay_seconds=2)

        elif rule_type == "immediate":
            print(f"[{agent_id}] Exit rule 'immediate' — scheduling shutdown")
            shutdown_fn(delay_seconds=2)

        else:
            # "persistent" or unknown — no auto-stop
            print(f"[{agent_id}] Exit rule '{rule_type}' — no auto-stop")

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse(metadata: dict) -> dict:
        """Extract the exit rule from message *metadata*.

        Returns a dict with ``type`` and ``ack_timeout_seconds`` keys,
        using defaults when the field is absent.
        """
        rule = (metadata or {}).get("exitRule") or {}
        return {
            "type": str(rule.get("type") or "wait_for_parent_ack"),
            "ack_timeout_seconds": int(
                rule.get("ack_timeout_seconds") or DEFAULT_ACK_TIMEOUT_SECONDS
            ),
        }

    @staticmethod
    def build(
        rule_type: str = "wait_for_parent_ack",
        ack_timeout_seconds: int = DEFAULT_ACK_TIMEOUT_SECONDS,
    ) -> dict:
        """Build an exit rule dict to embed in child-agent message metadata."""
        return {
            "type": rule_type,
            "ack_timeout_seconds": ack_timeout_seconds,
        }
