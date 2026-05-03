"""File-persistent startup backoff for dev-agent containers.

Prevents crash loops from consuming host resources by enforcing an
exponential delay before each successive failed start.

Usage (in container entrypoint)::

    from common.startup_backoff import enforce_startup_backoff, reset_startup_backoff

    enforce_startup_backoff()   # may sleep before proceeding
    try:
        main()
        reset_startup_backoff() # successful exit — clear counter
    except Exception:
        raise   # counter is NOT reset; next start will back off longer
"""

from __future__ import annotations

import json
import os
import time

# Backoff schedule in seconds: first two starts are immediate, then escalate.
BACKOFF_SCHEDULE: list[int] = [0, 0, 10, 30, 120, 300, 900]

_DEFAULT_STATE_FILE = "/tmp/startup-backoff.json"


def _state_file() -> str:
    return os.environ.get("STARTUP_BACKOFF_STATE_FILE", _DEFAULT_STATE_FILE)


def _read_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_state(path: str, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def enforce_startup_backoff(*, state_file: str | None = None) -> int:
    """Read crash counter, wait if needed, then increment counter.

    Returns the number of seconds slept (0 if no delay).
    """
    path = state_file or _state_file()
    state = _read_state(path)
    attempt = state.get("attempt", 0) + 1
    idx = min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)
    delay = BACKOFF_SCHEDULE[idx]
    _write_state(path, {"attempt": attempt, "timestamp": time.time()})
    if delay > 0:
        print(
            f"[startup-backoff] attempt={attempt}, waiting {delay}s before start.",
            flush=True,
        )
        time.sleep(delay)
    return delay


def reset_startup_backoff(*, state_file: str | None = None) -> None:
    """Clear the crash counter after a successful run."""
    path = state_file or _state_file()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def current_attempt(*, state_file: str | None = None) -> int:
    """Return the current attempt count (0 if no prior crashes)."""
    path = state_file or _state_file()
    return _read_state(path).get("attempt", 0)
