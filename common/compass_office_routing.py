"""Office routing helpers for the Compass Agent.

Handles multi-turn clarification for office/document tasks before the main
agentic workflow starts.  These helpers are extracted from compass/app.py to
keep the HTTP handler thin.

Clarification flow for office tasks:
  1. Validate target paths (or ask user for them).
  2. Ask user to choose output mode: workspace-only (A) or in-place (B).
  3. If in-place, ask user to confirm write access.
  4. Build Docker bind context and start the agentic workflow.

All Python in this module is protocol/permissions logic.  The actual
interpretation of user replies (deciding workspace vs in-place) is delegated
to the LLM via the caller's ``interpret_reply_fn`` hook.
"""

from __future__ import annotations

import json
import os

OFFICE_CONTAINER_INPUT_PATH = "/app/userdata"
OFFICE_CONTAINER_WORKSPACE_PATH = "/app/workspace"


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

def is_containerized() -> bool:
    """Return True when running inside a container."""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "rb") as fh:
            content = fh.read(4096).decode("ascii", errors="replace")
            if any(m in content for m in ("docker", "containerd", "/lxc/")):
                return True
    except OSError:
        pass
    return False


def can_defer_office_path_existence_check(path: str) -> bool:
    """Return True if the path existence check can be skipped (running in container)."""
    return is_containerized() and os.path.isabs(path)


def path_within_base(path: str, base: str) -> bool:
    """Return True if *path* is under *base* after resolving symlinks."""
    try:
        common = os.path.commonpath([os.path.realpath(path), os.path.realpath(base)])
    except ValueError:
        return False
    return common == os.path.realpath(base)


def validate_office_target_paths(
    target_paths: list[str],
    allowed_base_paths: list[str] | None = None,
) -> tuple[list[str], str]:
    """Validate a list of absolute file/folder paths for office use.

    Returns ``(normalized_paths, error_message)``.  On success, error_message
    is empty.  On failure, normalized_paths is empty and error_message describes
    the problem.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in target_paths or []:
        path = str(raw_path or "").strip()
        if not path:
            continue
        if not os.path.isabs(path):
            return [], f"Path must be absolute: {path}"
        real_path = os.path.realpath(path)
        if allowed_base_paths and not any(
            path_within_base(real_path, base) for base in allowed_base_paths
        ):
            return [], f"Path is outside OFFICE_ALLOWED_BASE_PATHS: {path}"
        if not os.path.exists(real_path):
            if can_defer_office_path_existence_check(real_path):
                if real_path not in seen:
                    seen.add(real_path)
                    normalized.append(real_path)
                continue
            return [], f"Path does not exist: {path}"
        if real_path not in seen:
            seen.add(real_path)
            normalized.append(real_path)
    return normalized, ""


# ---------------------------------------------------------------------------
# Question builders
# ---------------------------------------------------------------------------

def build_output_target_question(paths: list[str]) -> str:
    joined = "\n".join(f"- {p}" for p in paths)
    return (
        "Choose where the Office task should write its output:\n"
        "[A] workspace only (recommended, source stays read-only)\n"
        "[B] modify the original location directly (requires write permission)\n\n"
        f"Target path(s):\n{joined}"
    )


def build_write_permission_question(paths: list[str]) -> str:
    joined = "\n".join(f"- {p}" for p in paths)
    return (
        "This Office task will modify the original location directly. Approve write access?\n"
        "Reply yes to continue or no to stop.\n\n"
        f"Target path(s):\n{joined}"
    )


# ---------------------------------------------------------------------------
# Docker bind context builder
# ---------------------------------------------------------------------------

def build_office_dispatch_context(
    target_paths: list[str],
    output_mode: str,
    workspace_host_path: str = "",
) -> dict:
    """Build the Docker bind and mount context for launching an Office agent.

    Returns a dict with keys: mountRootHostPath, mountedTargetPaths,
    workspaceHostPath, extraBinds, readMode.
    """
    if not target_paths:
        raise ValueError("Office routing requires at least one target path.")

    mount_roots = [
        p if os.path.isdir(p) else os.path.dirname(p)
        for p in target_paths
    ]
    mount_root = os.path.commonpath(mount_roots)
    read_mode = "rw" if output_mode == "inplace" else "ro"

    mounted_targets = []
    for host_path in target_paths:
        relative = os.path.relpath(host_path, mount_root)
        mounted_targets.append(os.path.join(OFFICE_CONTAINER_INPUT_PATH, relative))

    extra_binds = [f"{mount_root}:{OFFICE_CONTAINER_INPUT_PATH}:{read_mode}"]
    if workspace_host_path:
        extra_binds.append(f"{workspace_host_path}:{OFFICE_CONTAINER_WORKSPACE_PATH}:rw")

    return {
        "mountRootHostPath": mount_root,
        "mountedTargetPaths": mounted_targets,
        "workspaceHostPath": workspace_host_path,
        "extraBinds": extra_binds,
        "readMode": read_mode,
    }


# ---------------------------------------------------------------------------
# Multi-turn state machine
# ---------------------------------------------------------------------------

def resume_office_clarification(
    prior_task_state: dict,
    user_reply: str,
    *,
    interpret_reply_fn,
    validate_paths_fn=None,
) -> dict:
    """Advance the office pre-flight clarification state machine.

    ``prior_task_state`` is the current ``router_context`` dict stored on the
    task.  ``interpret_reply_fn(question_context, user_reply) -> action_dict``
    is called for steps that require LLM interpretation.

    ``validate_paths_fn`` defaults to ``validate_office_target_paths``.

    Returns a response dict:
    ```
    {
        "action": "input_required" | "dispatch" | "error",
        "question": str,            # when action == "input_required"
        "router_context": dict,     # updated state
        "error": str,               # when action == "error"
    }
    ```
    """
    if validate_paths_fn is None:
        validate_paths_fn = validate_office_target_paths

    router_context = dict(prior_task_state or {})
    awaiting_step = router_context.get("awaitingStep") or ""

    # ---- clarify_path ----
    if awaiting_step == "clarify_path":
        # The user has provided additional context; re-validate paths from their reply.
        # We return a sentinel that tells the caller to re-run routing with combined text.
        return {
            "action": "re_route",
            "router_context": router_context,
        }

    # ---- output_mode ----
    if awaiting_step == "output_mode":
        target_paths = [
            str(p) for p in (router_context.get("targetPaths") or []) if str(p).strip()
        ]
        decision = interpret_reply_fn(
            {
                "awaitingStep": "output_mode",
                "question": router_context.get("currentQuestion") or "",
                "targetPaths": target_paths,
            },
            user_reply,
        )
        action = str(decision.get("action") or "unclear").lower()

        if action == "workspace":
            router_context["outputMode"] = "workspace"
            router_context["awaitingStep"] = "ready"
            return {"action": "dispatch", "router_context": router_context}

        if action == "inplace":
            router_context["outputMode"] = "inplace"
            router_context["awaitingStep"] = "confirm_write"
            question = build_write_permission_question(target_paths)
            router_context["currentQuestion"] = question
            return {
                "action": "input_required",
                "question": question,
                "router_context": router_context,
            }

        question = decision.get("clarification_question") or "Please choose workspace [A] or in-place [B] output."
        router_context["currentQuestion"] = question
        return {
            "action": "input_required",
            "question": question,
            "router_context": router_context,
        }

    # ---- confirm_write ----
    if awaiting_step == "confirm_write":
        target_paths = [
            str(p) for p in (router_context.get("targetPaths") or []) if str(p).strip()
        ]
        decision = interpret_reply_fn(
            {
                "awaitingStep": "confirm_write",
                "question": router_context.get("currentQuestion") or "",
                "targetPaths": target_paths,
            },
            user_reply,
        )
        action = str(decision.get("action") or "unclear").lower()

        if action == "approve":
            router_context["outputMode"] = "inplace"
            router_context["officeWriteApproved"] = True
            router_context["awaitingStep"] = "ready"
            return {"action": "dispatch", "router_context": router_context}

        if action == "deny":
            router_context["outputMode"] = "workspace"
            router_context.pop("officeWriteApproved", None)
            router_context["awaitingStep"] = "ready"
            return {
                "action": "dispatch",
                "router_context": router_context,
                "note": "Write access denied — continuing with workspace-only output.",
            }

        question = decision.get("clarification_question") or "Please reply yes to approve write access or no to stop."
        router_context["currentQuestion"] = question
        return {
            "action": "input_required",
            "question": question,
            "router_context": router_context,
        }

    # Unknown state
    return {
        "action": "error",
        "error": f"Unknown office awaiting_step: '{awaiting_step}'",
        "router_context": router_context,
    }
