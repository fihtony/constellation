"""Runtime HTTP circuit breaker.

Prevents cascading failures when a downstream agent is repeatedly failing.
Use this wrapper around A2A HTTP calls to avoid flooding a sick service.

States:
    closed  — normal; calls pass through.
    open    — tripped; calls fail immediately with ``CircuitOpenError``.
    half-open — trial; one call is allowed through to test recovery.

Usage::

    from common.circuit_breaker import CircuitBreaker, CircuitOpenError

    breaker = CircuitBreaker(name="scm-agent", failure_threshold=3, reset_timeout=60)

    try:
        result = breaker.call(make_scm_request, ...)
    except CircuitOpenError as exc:
        # circuit is open — report degraded state, don't retry
        ...
"""

from __future__ import annotations

import threading
import time


class CircuitOpenError(Exception):
    """Raised when a call is blocked because the circuit is open."""


class CircuitBreaker:
    """Thread-safe circuit breaker."""

    def __init__(
        self,
        name: str = "unknown",
        failure_threshold: int = 3,
        reset_timeout: float = 60.0,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._lock = threading.Lock()
        self._failures = 0
        self._state = "closed"   # "closed" | "open" | "half-open"
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._effective_state()

    def _effective_state(self) -> str:
        """Must be called with self._lock held."""
        if self._state == "open" and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.reset_timeout:
                self._state = "half-open"
        return self._state

    def call(self, fn, *args, **kwargs):
        """Execute *fn* if the circuit allows; otherwise raise ``CircuitOpenError``."""
        with self._lock:
            state = self._effective_state()
            if state == "open":
                raise CircuitOpenError(
                    f"Circuit '{self.name}' is open. "
                    f"Retry after {self.reset_timeout}s."
                )

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "closed"
            self._opened_at = None

    def _on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        """Manually reset the breaker to closed state."""
        with self._lock:
            self._failures = 0
            self._state = "closed"
            self._opened_at = None

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, state={self.state!r}, "
            f"failures={self._failures}/{self.failure_threshold})"
        )
