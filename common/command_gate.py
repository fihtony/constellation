"""Host-side command gate for Compass message ingestion.

Filters slash commands before they reach the task-creation pipeline.
Security policies are enforced at the control-plane boundary, not inside
agent containers, so a buggy or compromised container cannot bypass them.

Usage::

    from common.command_gate import gate_message, GateResult

    result = gate_message(text, user_id=user_id, role=role)
    if result == GateResult.FILTER:
        return  # silently discard
    elif result == GateResult.DENY:
        return send_error("Command not permitted.")
    # GateResult.PASS — proceed normally
"""

from __future__ import annotations

import re
from enum import Enum


class GateResult(Enum):
    PASS = "pass"
    FILTER = "filter"   # silently discard (client-side commands handled elsewhere)
    DENY = "deny"       # reject with error response


# Commands handled by the client / SDK — silently swallow if they reach the server.
FILTERED_COMMANDS: frozenset[str] = frozenset({
    "/help",
    "/login",
    "/logout",
    "/doctor",
    "/version",
})

# Commands that require the caller to have an elevated role.
ADMIN_COMMANDS: frozenset[str] = frozenset({
    "/clear",
    "/reset",
    "/context",
    "/debug",
    "/compact",
    "/cost",
})

ADMIN_ROLES: frozenset[str] = frozenset({"admin", "owner", "tech-lead"})


def gate_message(
    text: str,
    *,
    user_id: str | None = None,
    role: str = "user",
) -> GateResult:
    """Evaluate *text* against the command gate policy.

    Args:
        text: Raw message text.
        user_id: Optional caller identity (for logging).
        role: Caller's role string (e.g. "user", "admin", "owner").

    Returns:
        ``GateResult.PASS`` for normal messages.
        ``GateResult.FILTER`` for client-side slash commands.
        ``GateResult.DENY`` for admin commands issued by non-admin callers.
    """
    del user_id  # reserved for audit logging in future
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return GateResult.PASS

    command = _extract_command(stripped)
    if command in FILTERED_COMMANDS:
        return GateResult.FILTER
    if command in ADMIN_COMMANDS:
        if role not in ADMIN_ROLES:
            return GateResult.DENY
    return GateResult.PASS


def _extract_command(text: str) -> str:
    """Return the first token of a slash command, lower-cased."""
    match = re.match(r"^(/\S+)", text)
    return match.group(1).lower() if match else ""
