"""Common control tools for all Constellation agents.

These tools represent the agent lifecycle interface: dispatching tasks to other
agents, waiting for results, acknowledging completion, completing or failing the
current task, querying task context, and requesting user/agent clarification.

All tools self-register on import and work with any agentic runtime backend.
"""

from __future__ import annotations

import json
import os
import time
import threading
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
_ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))
_TASK_POLL_INTERVAL = 5  # seconds


# ---------------------------------------------------------------------------
# Shared state — configured by the agent wrapper before run_agentic()
# ---------------------------------------------------------------------------
_task_context: dict = {}
_complete_fn: Callable[[str, list], None] | None = None
_fail_fn: Callable[[str], None] | None = None
_input_required_fn: Callable[[str, str | None], None] | None = None
_wait_for_input_fn: Callable[[str], str | None] | None = None

# Sentinel so callers can explicitly pass None to clear a callback
_UNSET = object()


def configure_control_tools(
    *,
    task_context: Any = _UNSET,
    complete_fn: Any = _UNSET,
    fail_fn: Any = _UNSET,
    input_required_fn: Any = _UNSET,
    wait_for_input_fn: Any = _UNSET,
) -> None:
    """Wire up lifecycle callbacks for control tools.

    Called by the agent host (app.py) before invoking run_agentic().
    Pass ``None`` explicitly to clear a callback; omit a parameter to leave it
    unchanged.

    Args:
        task_context: dict with taskId, permissions, workspacePath, etc.
        complete_fn: callback(result_text, artifacts) to mark task COMPLETED
        fail_fn: callback(error_message) to mark task FAILED
        input_required_fn: callback(question, context) to request user input
        wait_for_input_fn: callback(question) -> user_reply or None (blocking)
    """
    global _task_context, _complete_fn, _fail_fn, _input_required_fn, _wait_for_input_fn
    if task_context is not _UNSET:
        _task_context = task_context or {}
    if complete_fn is not _UNSET:
        _complete_fn = complete_fn
    if fail_fn is not _UNSET:
        _fail_fn = fail_fn
    if input_required_fn is not _UNSET:
        _input_required_fn = input_required_fn
    if wait_for_input_fn is not _UNSET:
        _wait_for_input_fn = wait_for_input_fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _discover_capability_url(capability: str) -> str | None:
    """Discover an agent URL by capability via the Registry."""
    try:
        req = Request(
            f"{_REGISTRY_URL}/query?capability={capability}",
            headers={"Accept": "application/json"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list):
            agents = data
        elif isinstance(data, dict):
            agents = data.get("agents") or data.get("items") or []
        else:
            agents = []
        for agent in agents:
            instances = agent.get("instances") or []
            for inst in instances:
                url = (
                    inst.get("url")
                    or inst.get("serviceUrl")
                    or inst.get("service_url")
                    or agent.get("baseUrl")
                    or agent.get("serviceUrl")
                    or agent.get("service_url")
                )
                if url:
                    return url.rstrip("/")
            card_url = str(agent.get("card_url") or agent.get("cardUrl") or "").strip()
            if card_url.startswith("http://") or card_url.startswith("https://"):
                return card_url.rsplit("/.well-known/agent-card.json", 1)[0].rstrip("/")
        return None
    except Exception:  # noqa: BLE001
        return None


def _a2a_dispatch(
    agent_url: str,
    capability: str,
    task_text: str,
    metadata: dict | None = None,
    context_id: str | None = None,
) -> dict:
    """Send an A2A message:send request and return the response."""
    msg_id = f"tool-dispatch-{int(time.time() * 1000)}"
    msg: dict[str, Any] = {
        "messageId": msg_id,
        "role": "ROLE_USER",
        "parts": [{"text": task_text}],
        "metadata": {
            "requestedCapability": capability,
            **(metadata or {}),
        },
    }
    if context_id:
        msg["contextId"] = context_id
    payload = {
        "message": msg,
        "configuration": {"returnImmediately": True},
    }
    req = Request(
        f"{agent_url}/message:send",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(req, timeout=_ACK_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _poll_task_until_done(agent_url: str, task_id: str, timeout: int = 600) -> dict:
    """Poll GET /tasks/{task_id} until terminal state or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = Request(
                f"{agent_url}/tasks/{task_id}",
                headers={"Accept": "application/json"},
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            task = data.get("task") or data
            state = (task.get("status") or {}).get("state") or task.get("state") or ""
            if state in (
                "TASK_STATE_COMPLETED",
                "TASK_STATE_FAILED",
                "TASK_STATE_INPUT_REQUIRED",
                "completed",
                "failed",
                "input_required",
            ):
                return task
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_TASK_POLL_INTERVAL)
    return {"state": "TASK_STATE_FAILED", "error": "timeout waiting for agent task"}


# ---------------------------------------------------------------------------
# dispatch_agent_task
# ---------------------------------------------------------------------------

class DispatchAgentTaskTool(ConstellationTool):
    """Generic version of dispatch_dev_agent — works with any agent capability."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="dispatch_agent_task",
            description=(
                "Dispatch a task to another Constellation agent identified by capability. "
                "The Registry is used for discovery. Returns the task_id and agent_url so "
                "you can later call wait_for_agent_task and ack_agent_task."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "capability": {
                        "type": "string",
                        "description": "Agent capability to dispatch to, e.g. 'android.task.execute'",
                    },
                    "task_text": {
                        "type": "string",
                        "description": "Task description or instruction for the target agent.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": (
                            "Optional A2A metadata passed to the target agent, such as "
                            "jiraContext, designContext, scmContext, sharedWorkspacePath, permissions."
                        ),
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Optional parent context ID for task threading.",
                    },
                },
                "required": ["capability", "task_text"],
            },
        )

    def execute(self, args: dict) -> dict:
        capability = str(args.get("capability") or "").strip()
        task_text = str(args.get("task_text") or "").strip()
        metadata = args.get("metadata") or {}
        context_id = str(args.get("context_id") or "").strip() or None

        if not capability:
            return self.error("Missing required argument: capability")
        if not task_text:
            return self.error("Missing required argument: task_text")

        agent_url = _discover_capability_url(capability)
        if not agent_url:
            return self.error(
                f"No agent available for capability '{capability}'. "
                "The agent may not be registered or may be unavailable."
            )

        try:
            resp = _a2a_dispatch(agent_url, capability, task_text, metadata, context_id)
        except (URLError, OSError) as exc:
            return self.error(f"Dispatch failed for capability '{capability}': {exc}")

        task = resp.get("task") or {}
        task_id = task.get("id") or task.get("taskId") or ""
        return self.ok(
            json.dumps(
                {
                    "taskId": task_id,
                    "agentUrl": agent_url,
                    "capability": capability,
                    "initialState": (task.get("status") or {}).get("state") or "submitted",
                },
                ensure_ascii=False,
            )
        )


# ---------------------------------------------------------------------------
# wait_for_agent_task
# ---------------------------------------------------------------------------

class WaitForAgentTaskTool(ConstellationTool):
    """Poll an agent task until it reaches a terminal state."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="wait_for_agent_task",
            description=(
                "Wait for an agent task (dispatched via dispatch_agent_task) to complete. "
                "Polls the agent until it reaches COMPLETED, FAILED, or INPUT_REQUIRED state. "
                "Returns the final task status and artifacts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_url": {
                        "type": "string",
                        "description": "Base URL of the agent (returned by dispatch_agent_task).",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task ID returned by dispatch_agent_task.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Maximum seconds to wait (default: 600).",
                    },
                },
                "required": ["agent_url", "task_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        agent_url = str(args.get("agent_url") or "").strip().rstrip("/")
        task_id = str(args.get("task_id") or "").strip()
        timeout = int(args.get("timeout") or 600)

        if not agent_url:
            return self.error("Missing required argument: agent_url")
        if not task_id:
            return self.error("Missing required argument: task_id")

        task = _poll_task_until_done(agent_url, task_id, timeout=timeout)
        # Surface timeout as an error so the LLM knows the task did not complete
        if task.get("error") and "timeout" in task.get("error", ""):
            return self.error(
                f"Timed out waiting for task '{task_id}' after {timeout}s. "
                "The agent may still be working."
            )
        return self.ok(json.dumps(task, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# ack_agent_task
# ---------------------------------------------------------------------------

class AckAgentTaskTool(ConstellationTool):
    """Send ACK to a per-task agent when all review cycles are done."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ack_agent_task",
            description=(
                "Send an ACK signal to a per-task agent after all review cycles are complete. "
                "This allows the agent to proceed with graceful shutdown per the per-task exit rule."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_url": {
                        "type": "string",
                        "description": "Base URL of the agent to ACK.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task ID to ACK.",
                    },
                },
                "required": ["agent_url", "task_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        agent_url = str(args.get("agent_url") or "").strip().rstrip("/")
        task_id = str(args.get("task_id") or "").strip()

        if not agent_url:
            return self.error("Missing required argument: agent_url")
        if not task_id:
            return self.error("Missing required argument: task_id")

        try:
            req = Request(
                f"{agent_url}/tasks/{task_id}/ack",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"ACK failed for task '{task_id}' at '{agent_url}': {exc}")


# ---------------------------------------------------------------------------
# complete_current_task
# ---------------------------------------------------------------------------

class CompleteCurrentTaskTool(ConstellationTool):
    """Signal the agent runtime to mark the current task as COMPLETED."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="complete_current_task",
            description=(
                "Mark the current task as successfully completed. "
                "Call this when all work is done and the deliverable is ready. "
                "Provide a result_text summary and optional artifact list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "result_text": {
                        "type": "string",
                        "description": "Human-readable summary of what was accomplished.",
                    },
                    "artifacts": {
                        "type": "array",
                        "description": "Optional list of artifact objects to include in the result.",
                        "items": {"type": "object"},
                    },
                },
                "required": ["result_text"],
            },
        )

    def execute(self, args: dict) -> dict:
        result_text = str(args.get("result_text") or "").strip()
        artifacts = list(args.get("artifacts") or [])

        if not result_text:
            return self.error("Missing required argument: result_text")

        if _complete_fn is not None:
            try:
                _complete_fn(result_text, artifacts)
                return self.ok(f"Task completion signaled: {result_text[:200]}")
            except Exception as exc:  # noqa: BLE001
                return self.error(f"Failed to signal task completion: {exc}")

        # Fallback: return structured signal that the runtime wrapper can detect
        return self.ok(
            json.dumps(
                {
                    "__signal__": "complete_task",
                    "result_text": result_text,
                    "artifacts": artifacts,
                },
                ensure_ascii=False,
            )
        )


# ---------------------------------------------------------------------------
# fail_current_task
# ---------------------------------------------------------------------------

class FailCurrentTaskTool(ConstellationTool):
    """Signal the agent runtime to mark the current task as FAILED."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="fail_current_task",
            description=(
                "Mark the current task as failed. "
                "Call this when the task cannot be completed due to an unrecoverable error. "
                "Include a clear error_message explaining what went wrong."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "error_message": {
                        "type": "string",
                        "description": "Human-readable description of why the task failed.",
                    },
                    "error_type": {
                        "type": "string",
                        "description": (
                            "Optional error category: tool_error | permission_error | "
                            "boundary_error | orchestration_error | validation_error | runtime_error"
                        ),
                    },
                    "retriable": {
                        "type": "boolean",
                        "description": "Whether retrying the task might succeed (default false).",
                    },
                },
                "required": ["error_message"],
            },
        )

    def execute(self, args: dict) -> dict:
        error_message = str(args.get("error_message") or "").strip()
        error_type = str(args.get("error_type") or "").strip()
        retriable = bool(args.get("retriable", False))

        if not error_message:
            return self.error("Missing required argument: error_message")

        if _fail_fn is not None:
            try:
                _fail_fn(error_message)
                return self.ok(f"Task failure signaled: {error_message[:200]}")
            except Exception as exc:  # noqa: BLE001
                return self.error(f"Failed to signal task failure: {exc}")

        return self.ok(
            json.dumps(
                {
                    "__signal__": "fail_task",
                    "error_message": error_message,
                    "error_type": error_type,
                    "retriable": retriable,
                },
                ensure_ascii=False,
            )
        )


# ---------------------------------------------------------------------------
# get_task_context
# ---------------------------------------------------------------------------

class GetTaskContextTool(ConstellationTool):
    """Return current task metadata: ID, permissions, workspace path, etc."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="get_task_context",
            description=(
                "Get the current task context including task ID, permissions snapshot, "
                "shared workspace path, Jira context, design context, and SCM context "
                "as provided by the orchestrator."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    def execute(self, args: dict) -> dict:
        del args
        # Combine module-level config with live env vars for key fields
        ctx = dict(_task_context)
        if "taskId" not in ctx:
            ctx["taskId"] = os.environ.get("TASK_ID", "")
        if "workspacePath" not in ctx:
            ctx["workspacePath"] = os.environ.get("SHARED_WORKSPACE_PATH", "")
        return self.ok(json.dumps(ctx, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# get_agent_runtime_status
# ---------------------------------------------------------------------------

class GetAgentRuntimeStatusTool(ConstellationTool):
    """Return current backend name, readiness, and any limitations."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="get_agent_runtime_status",
            description=(
                "Get the current agentic runtime backend status including backend name, "
                "readiness, model, and any known limitations or errors."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    def execute(self, args: dict) -> dict:
        del args
        try:
            from common.runtime.adapter import summarize_runtime_configuration
            summary = summarize_runtime_configuration()
            return self.ok(json.dumps(summary, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            return self.error(f"Failed to get runtime status: {exc}")


# ---------------------------------------------------------------------------
# request_user_input
# ---------------------------------------------------------------------------

class RequestUserInputTool(ConstellationTool):
    """Pause execution and ask the user a clarifying question (INPUT_REQUIRED)."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="request_user_input",
            description=(
                "Pause the current task and ask the user a clarifying question. "
                "Use this ONLY when: (1) Team Lead cannot answer via existing context, "
                "(2) the question would block execution, and (3) it is not an implementation detail. "
                "Prefer request_agent_clarification for questions the orchestrator can answer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Clear, concise question for the user.",
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Optional context explaining why this information is needed "
                            "and what options are available."
                        ),
                    },
                },
                "required": ["question"],
            },
        )

    def execute(self, args: dict) -> dict:
        question = str(args.get("question") or "").strip()
        context = str(args.get("context") or "").strip()

        if not question:
            return self.error("Missing required argument: question")

        # If a blocking wait function is configured, use it to get the user's reply
        if _wait_for_input_fn is not None:
            try:
                full_question = question
                if context:
                    full_question = f"{question}\n\nContext: {context}"
                user_reply = _wait_for_input_fn(full_question)
                if user_reply is None:
                    return self.error("User input request timed out. The user did not respond in time.")
                return self.ok(f"User replied: {user_reply}")
            except Exception as exc:  # noqa: BLE001
                return self.error(f"Failed to get user input: {exc}")

        # Non-blocking: just signal INPUT_REQUIRED to orchestrator
        if _input_required_fn is not None:
            try:
                _input_required_fn(question, context if context else None)
                return self.ok("INPUT_REQUIRED signaled to orchestrator. The workflow will pause until user responds.")
            except Exception as exc:  # noqa: BLE001
                return self.error(f"Failed to signal INPUT_REQUIRED: {exc}")

        return self.ok(
            json.dumps(
                {
                    "__signal__": "input_required",
                    "question": question,
                    "context": context,
                },
                ensure_ascii=False,
            )
        )


# ---------------------------------------------------------------------------
# request_agent_clarification
# ---------------------------------------------------------------------------

class RequestAgentClarificationTool(ConstellationTool):
    """Ask a cooperating agent (e.g. Team Lead, orchestrator) for clarification."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="request_agent_clarification",
            description=(
                "Ask a cooperating or upstream agent to clarify something before proceeding. "
                "Use capability to identify the target agent. "
                "Prefer this over request_user_input when the orchestrator may already have the answer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Clarification question to send to the target agent.",
                    },
                    "target_capability": {
                        "type": "string",
                        "description": (
                            "Capability of the agent to ask, e.g. 'team-lead.task.analyze'. "
                            "Defaults to orchestrator.user.interact if not specified."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context to help the target agent answer.",
                    },
                },
                "required": ["question"],
            },
        )

    def execute(self, args: dict) -> dict:
        question = str(args.get("question") or "").strip()
        target_capability = str(args.get("target_capability") or "orchestrator.user.interact").strip()
        context = str(args.get("context") or "").strip()

        if not question:
            return self.error("Missing required argument: question")

        agent_url = _discover_capability_url(target_capability)
        if not agent_url:
            return self.error(
                f"No agent available for capability '{target_capability}'. "
                "Cannot send clarification request."
            )

        task_text = question
        if context:
            task_text = f"{question}\n\nContext:\n{context}"

        try:
            resp = _a2a_dispatch(agent_url, target_capability, task_text)
            task = resp.get("task") or {}
            task_id = task.get("id") or task.get("taskId") or ""
            return self.ok(
                json.dumps(
                    {
                        "sent": True,
                        "targetCapability": target_capability,
                        "agentUrl": agent_url,
                        "taskId": task_id,
                        "note": "Clarification request dispatched. Use wait_for_agent_task if you need the answer before continuing.",
                    },
                    ensure_ascii=False,
                )
            )
        except (URLError, OSError) as exc:
            return self.error(f"Clarification request failed: {exc}")


# ---------------------------------------------------------------------------
# aggregate_task_card
# ---------------------------------------------------------------------------

class AggregateTaskCardTool(ConstellationTool):
    """Aggregate task evidence from artifacts and workspace into a structured card.

    Reads Team Lead's own workspace files and A2A artifacts to build a compact
    summary of task progress, PR evidence, Jira context, and completeness issues.
    Never reads execution-agent subdirectories directly.
    """

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="aggregate_task_card",
            description=(
                "Aggregate task evidence from A2A artifacts and Team Lead workspace files. "
                "Returns PR URL, branch, jiraInReview flag, completeness issues, and current phase. "
                "Use this to check whether a team-lead.task.analyze deliverable is complete before "
                "calling complete_current_task. Never reads execution-agent workspace files directly."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifacts": {
                        "type": "array",
                        "description": "A2A artifacts from the latest Team Lead callback.",
                        "items": {"type": "object"},
                    },
                },
                "required": ["artifacts"],
            },
        )

    def execute(self, args: dict) -> dict:
        artifacts = list(args.get("artifacts") or [])
        workspace_path = str(_task_context.get("workspacePath") or "")

        try:
            from compass.completeness import (
                extract_pr_evidence_from_artifacts,
                extract_team_lead_completeness_issues,
                derive_task_card_status,
            )
        except ImportError as exc:
            return self.error(f"compass_completeness module not available: {exc}")

        pr_evidence = extract_pr_evidence_from_artifacts(artifacts)
        issues = extract_team_lead_completeness_issues(workspace_path, artifacts)

        # Derive current phase from team-lead workspace
        current_phase = ""
        try:
            import json as _json
            import os as _os
            stage_path = _os.path.join(workspace_path, "team-lead", "stage-summary.json")
            if _os.path.isfile(stage_path):
                with open(stage_path, encoding="utf-8") as fh:
                    stage = _json.load(fh)
                current_phase = str(stage.get("currentPhase") or "")
        except Exception:  # noqa: BLE001
            pass

        card = {
            "prEvidence": pr_evidence,
            "completenessIssues": issues,
            "isComplete": len(issues) == 0,
            "currentPhase": current_phase,
            "artifactCount": len(artifacts),
        }
        return self.ok(json.dumps(card, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# derive_user_facing_status
# ---------------------------------------------------------------------------

class DeriveUserFacingStatusTool(ConstellationTool):
    """Derive the user-facing status label and kind from task state and artifacts."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="derive_user_facing_status",
            description=(
                "Derive the user-facing status label (e.g. 'Completed / PR Raised', "
                "'Completed / In Review', 'Failed', 'Waiting for Info') from the task "
                "state and PR evidence in artifacts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_state": {
                        "type": "string",
                        "description": "Current task state string, e.g. 'TASK_STATE_COMPLETED'.",
                    },
                    "artifacts": {
                        "type": "array",
                        "description": "A2A artifacts to extract PR evidence from.",
                        "items": {"type": "object"},
                    },
                },
                "required": ["task_state"],
            },
        )

    def execute(self, args: dict) -> dict:
        task_state = str(args.get("task_state") or "").strip()
        artifacts = list(args.get("artifacts") or [])

        try:
            from compass.completeness import (
                extract_pr_evidence_from_artifacts,
                derive_task_card_status,
            )
        except ImportError as exc:
            return self.error(f"compass_completeness module not available: {exc}")

        pr_evidence = extract_pr_evidence_from_artifacts(artifacts)
        status_kind, status_label = derive_task_card_status(task_state, pr_evidence)
        return self.ok(
            json.dumps(
                {"statusKind": status_kind, "statusLabel": status_label, "prEvidence": pr_evidence},
                ensure_ascii=False,
            )
        )


# ---------------------------------------------------------------------------
# Self-register all tools
# ---------------------------------------------------------------------------

register_tool(DispatchAgentTaskTool())
register_tool(WaitForAgentTaskTool())
register_tool(AckAgentTaskTool())
register_tool(CompleteCurrentTaskTool())
register_tool(FailCurrentTaskTool())
register_tool(GetTaskContextTool())
register_tool(GetAgentRuntimeStatusTool())
register_tool(RequestUserInputTool())
register_tool(RequestAgentClarificationTool())
register_tool(AggregateTaskCardTool())
register_tool(DeriveUserFacingStatusTool())


# ---------------------------------------------------------------------------
# validate_office_paths
# ---------------------------------------------------------------------------

class ValidateOfficePathsTool(ConstellationTool):
    """Validate office target paths and compute Docker bind mounts.

    Used by the Compass agentic workflow when routing office tasks.
    Returns the validated paths and the ``extra_binds`` list to pass to
    ``launch_per_task_agent`` so the Office Agent container can access the
    user's files.
    """

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="validate_office_paths",
            description=(
                "Validate one or more absolute file/directory paths for an office task "
                "and compute the Docker bind mounts needed to launch the Office Agent. "
                "Returns validatedPaths, extraBinds (for launch_per_task_agent), and "
                "an error message when paths are invalid."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Absolute host paths to validate.",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["workspace", "inplace"],
                        "description": "workspace = read-only; inplace = read-write.",
                    },
                    "workspace_host_path": {
                        "type": "string",
                        "description": "Host-side shared workspace path (optional).",
                    },
                },
                "required": ["target_paths"],
            },
        )

    def execute(self, args: dict) -> dict:
        from compass.office_routing import (
            validate_office_target_paths,
            build_office_dispatch_context,
        )

        raw_paths = [str(p).strip() for p in (args.get("target_paths") or []) if str(p).strip()]
        output_mode = str(args.get("output_mode") or "workspace").strip().lower()
        workspace_host_path = str(args.get("workspace_host_path") or "").strip()

        allowed_base_paths_env = os.environ.get("OFFICE_ALLOWED_BASE_PATHS", "")
        allowed_base_paths = [
            os.path.realpath(p.strip())
            for p in allowed_base_paths_env.split(":")
            if p.strip()
        ] or None

        validated, error = validate_office_target_paths(raw_paths, allowed_base_paths)

        if error:
            return self.error(f"Office path validation failed: {error}")

        if not validated:
            return self.error("No valid paths provided. Please supply absolute paths to files or directories.")

        try:
            dispatch_ctx = build_office_dispatch_context(
                validated,
                output_mode=output_mode,
                workspace_host_path=workspace_host_path,
            )
        except Exception as exc:  # noqa: BLE001
            return self.error(f"Failed to build office dispatch context: {exc}")

        return self.ok(
            json.dumps(
                {
                    "validatedPaths": validated,
                    "containerTargetPaths": dispatch_ctx["mountedTargetPaths"],
                    "extraBinds": dispatch_ctx["extraBinds"],
                    "outputMode": output_mode,
                    "readMode": dispatch_ctx["readMode"],
                },
                ensure_ascii=False,
            )
        )


register_tool(ValidateOfficePathsTool())
