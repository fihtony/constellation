"""Compass agent with browser UI, workflow routing, and on-demand launcher support."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import time
import uuid
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from common.artifact_store import ArtifactStore
from common.compass_agentic_workflow import run_compass_workflow
from common.devlog import record_workspace_stage
from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.launcher import get_launcher
from common.message_utils import artifact_text, deep_copy_json, extract_text, parse_json_object
from common.policy import PolicyEvaluator
from common.per_task_exit import PerTaskExitHandler
from common.registry_client import RegistryClient
from common.agent_system_prompt import build_agent_system_prompt as _build_manifest_prompt
from common.runtime.adapter import get_runtime, require_agentic_runtime, summarize_runtime_configuration
from common.task_permissions import grant_permission, load_permission_grant
from common.task_store import TaskStore
from common.time_utils import local_file_timestamp, local_iso_timestamp
from compass import prompts

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

AGENT_ID = os.environ.get("AGENT_ID", "compass-agent")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://localhost:{PORT}")
# Unique ID for this Compass process instance.  Scopes artifact folders and
# lets agents detect stale callbacks from a previous Compass instance.
COMPASS_INSTANCE_ID = os.environ.get("COMPASS_INSTANCE_ID") or str(uuid.uuid4())[:8]
ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", os.environ.get("A2A_READ_TIMEOUT_SECONDS", "15")))
DOWNSTREAM_TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "3600"))
UI_PATH = os.path.join(os.path.dirname(__file__), "ui", "index.html")
COMPASS_COMPLETENESS_MAX_REVISIONS = int(os.environ.get("COMPASS_COMPLETENESS_MAX_REVISIONS", "2"))
COMPASS_CHILD_ACK_TIMEOUT = int(os.environ.get("COMPASS_CHILD_ACK_TIMEOUT_SECONDS", "300"))
OFFICE_ALLOWED_BASE_PATHS = [
    os.path.realpath(path.strip())
    for path in os.environ.get("OFFICE_ALLOWED_BASE_PATHS", "").split(":")
    if path.strip()
]
OFFICE_CONTAINER_INPUT_PATH = "/app/userdata"
OFFICE_CONTAINER_WORKSPACE_PATH = "/app/workspace"

registry = RegistryClient()
task_store = TaskStore()
# Each Compass instance stores artifacts under its own subdirectory so that
# a restart with a reset task counter cannot mix files with previous runs.
_artifact_root_base = os.environ.get("ARTIFACT_ROOT", "/app/artifacts")
_artifact_root_instance = os.path.join(_artifact_root_base, f"compass-{COMPASS_INSTANCE_ID}")
artifact_store = ArtifactStore(root=_artifact_root_instance)
launcher = get_launcher()
policy = PolicyEvaluator()
COMPASS_API_KEY = os.environ.get("COMPASS_API_KEY", "").strip()

# Notification target registry (IM Gateway or other webhook subscribers)
_notification_targets_lock = threading.Lock()
_notification_targets: list[dict] = []  # [{"url": "http://...", "registeredAt": ...}]

TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
FIGMA_URL_RE = re.compile(r"https?://[^\s\"'\}]*figma\.com/[^\s\"'\}]+", re.IGNORECASE)
STITCH_URL_RE = re.compile(
    r"https?://[^\s\"'\}]*(?:stitch\.withgoogle\.com|stitch\.googleapis\.com)/[^\s\"'\}]+",
    re.IGNORECASE,
)
NON_TERMINAL_TASK_STATES = {
    "SUBMITTED",
    "ROUTING",
    "DISPATCHED",
    "STEP_IN_PROGRESS",
    "TASK_STATE_ACCEPTED",
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_RUNNING",
    "TASK_STATE_DISPATCHED",
    # Team Lead intermediate states
    "ANALYZING",
    "GATHERING_INFO",
    "PLANNING",
    "EXECUTING",
    "REVIEWING",
    "COMPLETING",
    # Web / Android agent intermediate states
    "IMPLEMENTING",
    "WRITING",
    "BUILDING",
    "PUSHING",
}
# States that trigger notification webhooks
_NOTIFY_STATES = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_COMPLETED", "TASK_STATE_FAILED"}


def _fire_notification(task):
    """POST task state change to all registered notification targets (best-effort)."""
    state = task.state
    if state not in _NOTIFY_STATES:
        return
    with _notification_targets_lock:
        targets = list(_notification_targets)
    if not targets:
        return
    payload = json.dumps({
        "taskId": task.task_id,
        "state": state,
        "statusMessage": task.status_message,
        "ownerUserId": task.owner_user_id,
        "tenantId": task.tenant_id,
        "sourceChannel": task.source_channel,
        "artifacts": task.artifacts[-5:] if task.artifacts else [],
        "summary": task.summary,
    }, ensure_ascii=False).encode("utf-8")
    for target in targets:
        url = target.get("url", "")
        if not url:
            continue
        try:
            req = Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urlopen(req, timeout=5):
                pass
        except Exception as err:
            print(f"[compass] Notification to {url} failed: {err}")


def _update_state_and_notify(task_id, state, status_message=""):
    """Update task state and fire notification webhooks for key states."""
    task = task_store.update_state(task_id, state, status_message)
    if task:
        threading.Thread(target=_fire_notification, args=(task,), daemon=True).start()
    return task


def audit_log(event, **kwargs):
    entry = {"ts": local_iso_timestamp(), "event": event, **kwargs}
    print(f"[audit] {json.dumps(entry, ensure_ascii=False)}")


def _runtime_config_summary():
    summary = {
        "service": "compass",
        "instanceId": COMPASS_INSTANCE_ID,
        "registryUrl": os.environ.get("REGISTRY_URL", "http://registry:9000"),
        "artifactRoot": artifact_store.root,
        "dynamicAgentNetwork": os.environ.get("DYNAMIC_AGENT_NETWORK", "constellation-network"),
        "ackTimeoutSeconds": ACK_TIMEOUT,
        "taskTimeoutSeconds": DOWNSTREAM_TASK_TIMEOUT,
    }
    summary["runtimeConfig"] = summarize_runtime_configuration()
    return summary


def _run_agentic(prompt, actor, *, system_prompt=None, context=None, timeout=120, max_tokens=2048):
    result = get_runtime().run(
        prompt=prompt,
        context=context,
        system_prompt=system_prompt,
        timeout=timeout,
        max_tokens=max_tokens,
    )
    for warning in result.get("warnings") or []:
        print(f"[compass] Runtime warning ({actor}): {warning}")
    return result.get("raw_response") or result.get("summary") or ""


def _normalize_workflow(items):
    workflow = []
    for item in items or []:
        value = str(item or "").strip()
        if value:
            workflow.append(value)
    return _dedupe(workflow)


def _is_office_capability(capability):
    return str(capability or "").startswith("office.")


def _path_within_base(path, base):
    try:
        common = os.path.commonpath([os.path.realpath(path), os.path.realpath(base)])
    except ValueError:
        return False
    return common == os.path.realpath(base)


def _is_containerized():
    """Return True when running inside a container (Docker Desktop, Rancher Desktop, etc.).

    Checks two independent signals so that both Docker (/.dockerenv) and
    Rancher Desktop in containerd mode (/proc/1/cgroup) are covered.
    """
    # Docker Desktop and Rancher Desktop (dockerd mode) create this marker file.
    if os.path.exists("/.dockerenv"):
        return True
    # Fallback for Rancher Desktop containerd mode and other OCI runtimes.
    try:
        with open("/proc/1/cgroup", "rb") as fh:
            content = fh.read(4096).decode("ascii", errors="replace")
            if any(m in content for m in ("docker", "containerd", "/lxc/")):
                return True
    except OSError:
        pass
    return False


def _can_defer_office_path_existence_check(path):
    # Inside a container, host-side paths (e.g. /Users/…) are not accessible,
    # so skip the existence check and defer validation to the Office agent.
    return _is_containerized() and os.path.isabs(path)


def _validate_office_target_paths(target_paths):
    normalized = []
    for raw_path in target_paths or []:
        path = str(raw_path or "").strip()
        if not path:
            continue
        if not os.path.isabs(path):
            return [], f"Path must be absolute: {path}"
        real_path = os.path.realpath(path)
        if OFFICE_ALLOWED_BASE_PATHS and not any(
            _path_within_base(real_path, base) for base in OFFICE_ALLOWED_BASE_PATHS
        ):
            return [], f"Path is outside OFFICE_ALLOWED_BASE_PATHS: {path}"
        if not os.path.exists(real_path):
            if _can_defer_office_path_existence_check(real_path):
                normalized.append(real_path)
                continue
            return [], f"Path does not exist: {path}"
        normalized.append(real_path)
    return _dedupe(normalized), ""


def _build_output_target_question(paths):
    joined = "\n".join(f"- {path}" for path in paths)
    return (
        "Choose where the Office task should write its output:\n"
        "[A] workspace only (recommended, source stays read-only)\n"
        "[B] modify the original location directly (requires write permission)\n\n"
        f"Target path(s):\n{joined}"
    )


def _build_write_permission_question(paths):
    joined = "\n".join(f"- {path}" for path in paths)
    return (
        "This Office task will modify the original location directly. Approve write access?\n"
        "Reply yes to continue or no to stop.\n\n"
        f"Target path(s):\n{joined}"
    )


def _route_input_required(task, question, router_context):
    task.router_context = dict(router_context or {})
    _update_state_and_notify(task.task_id, "TASK_STATE_INPUT_REQUIRED", question)
    task_store.add_progress_step(task.task_id, question, agent_id="compass-agent")
    return task.to_dict()


def _resolve_workspace_host_path(workspace_path):
    if not workspace_path:
        return ""
    try:
        return launcher.resolve_host_path(workspace_path)
    except Exception:
        return ""


def _build_office_dispatch_context(task):
    router_context = dict(getattr(task, "router_context", {}) or {})
    target_paths = [os.path.realpath(path) for path in router_context.get("targetPaths") or []]
    if not target_paths:
        raise ValueError("Office routing requires at least one target path.")

    mount_roots = [path if os.path.isdir(path) else os.path.dirname(path) for path in target_paths]
    mount_root = os.path.commonpath(mount_roots)
    read_mode = "rw" if router_context.get("outputMode") == "inplace" else "ro"
    workspace_host_path = _resolve_workspace_host_path(task.workspace_path)
    mounted_targets = []
    for host_path in target_paths:
        relative = os.path.relpath(host_path, mount_root)
        mounted_targets.append(os.path.join(OFFICE_CONTAINER_INPUT_PATH, relative))

    extra_binds = [f"{mount_root}:{OFFICE_CONTAINER_INPUT_PATH}:{read_mode}"]
    if workspace_host_path:
        extra_binds.append(f"{workspace_host_path}:{OFFICE_CONTAINER_WORKSPACE_PATH}:rw")

    router_context["dispatch"] = {
        "mountRootHostPath": mount_root,
        "mountedTargetPaths": mounted_targets,
        "workspaceHostPath": workspace_host_path,
        "extraBinds": extra_binds,
        "readMode": read_mode,
    }
    task.router_context = router_context
    return router_context


def _interpret_office_reply(task, user_reply):
    router_context = dict(getattr(task, "router_context", {}) or {})
    prompt = prompts.OFFICE_REPLY_TEMPLATE.format(
        original_request=extract_text(task.original_message or {}),
        awaiting_step=router_context.get("awaitingStep") or "",
        current_question=task.status_message or "",
        office_context=json.dumps(router_context, ensure_ascii=False, indent=2),
        user_reply=user_reply or "",
    )
    system = _build_manifest_prompt(__file__, prompts.OFFICE_REPLY_SYSTEM)
    response = _run_agentic(prompt, "office-reply", system_prompt=system)
    data = parse_json_object(response)
    return {
        "action": str(data.get("action") or "unclear").strip().lower() or "unclear",
        "clarification_question": str(data.get("clarification_question") or "").strip() or None,
    }


def _start_task_worker(task, message, workflow):
    task.pending_workflow = list(workflow)
    task.router_context = dict(getattr(task, "router_context", {}) or {})
    task_store.update_state(task.task_id, "ROUTING", f"Planned workflow: {', '.join(workflow)}")
    task_store.add_progress_step(
        task.task_id,
        f"Planned workflow: {', '.join(workflow)}",
        agent_id="compass-agent",
    )
    worker = threading.Thread(
        target=_run_workflow,
        args=(task.task_id, deep_copy_json(message), list(workflow)),
        daemon=True,
    )
    worker.start()
    return task.to_dict()


def _maybe_prepare_office_route(task, workflow, route_decision):
    if not workflow or not _is_office_capability(workflow[0]):
        return None

    validated_paths, error_message = _validate_office_target_paths(route_decision.get("target_paths") or [])
    if not validated_paths:
        question = route_decision.get("input_question") or error_message or "Please provide the absolute path for the Office task."
        return _route_input_required(
            task,
            question,
            {
                "kind": "office",
                "awaitingStep": "clarify_path",
                "requestedCapability": workflow[0],
            },
        )

    return _route_input_required(
        task,
        _build_output_target_question(validated_paths),
        {
            "kind": "office",
            "awaitingStep": "output_mode",
            "requestedCapability": workflow[0],
            "officeSubtype": route_decision.get("office_subtype"),
            "targetPaths": validated_paths,
        },
    )


def _resume_compass_routed_task(prior_task, message):
    router_context = dict(getattr(prior_task, "router_context", {}) or {})
    if not router_context or prior_task.downstream_task_id:
        return None

    user_reply = extract_text(message)
    awaiting_step = router_context.get("awaitingStep") or ""

    if awaiting_step == "clarify_path":
        original_text = extract_text(prior_task.original_message or {})
        combined_text = (original_text + "\n\n" + user_reply).strip() if original_text else user_reply
        combined_message = deep_copy_json(prior_task.original_message or message)
        combined_message["parts"] = [{"text": combined_text}]
        prior_task.original_message = deep_copy_json(combined_message)
        route_decision = _route_with_runtime(combined_text, requested_capability=router_context.get("requestedCapability") or "")
        workflow = route_decision.get("workflow") or [router_context.get("requestedCapability") or "team-lead.task.analyze"]
        prior_task.summary = _truncate_text(route_decision.get("summary") or combined_text, 180)
        if route_decision.get("needs_input") and not _is_office_capability(workflow[0]):
            return _route_input_required(
                prior_task,
                route_decision.get("input_question") or "Please clarify the request.",
                {
                    "kind": "general",
                    "awaitingStep": "clarify_path",
                    "requestedCapability": workflow[0],
                },
            )
        office_response = _maybe_prepare_office_route(prior_task, workflow, route_decision)
        if office_response is not None:
            return office_response
        return _start_task_worker(prior_task, combined_message, workflow)

    if awaiting_step == "output_mode":
        decision = _interpret_office_reply(prior_task, user_reply)
        if decision["action"] == "workspace":
            router_context["outputMode"] = "workspace"
            prior_task.router_context = router_context
            _build_office_dispatch_context(prior_task)
            return _start_task_worker(prior_task, prior_task.original_message or message, prior_task.pending_workflow or [router_context.get("requestedCapability")])
        if decision["action"] == "inplace":
            router_context["outputMode"] = "inplace"
            router_context["awaitingStep"] = "confirm_write"
            return _route_input_required(
                prior_task,
                _build_write_permission_question(router_context.get("targetPaths") or []),
                router_context,
            )
        return _route_input_required(
            prior_task,
            decision["clarification_question"] or "Please choose workspace or in-place output.",
            router_context,
        )

    if awaiting_step == "confirm_write":
        decision = _interpret_office_reply(prior_task, user_reply)
        if decision["action"] == "approve":
            router_context["outputMode"] = "inplace"
            router_context["officeWriteApproved"] = True
            prior_task.router_context = router_context
            _build_office_dispatch_context(prior_task)
            return _start_task_worker(prior_task, prior_task.original_message or message, prior_task.pending_workflow or [router_context.get("requestedCapability")])
        if decision["action"] == "deny":
            router_context["outputMode"] = "workspace"
            router_context.pop("officeWriteApproved", None)
            prior_task.router_context = router_context
            _build_office_dispatch_context(prior_task)
            task_store.add_progress_step(
                prior_task.task_id,
                "Write access denied by user — continuing with workspace-only output.",
                agent_id="compass-agent",
            )
            return _start_task_worker(
                prior_task,
                prior_task.original_message or message,
                prior_task.pending_workflow or [router_context.get("requestedCapability")],
            )
        return _route_input_required(
            prior_task,
            decision["clarification_question"] or "Please reply yes to approve write access or no to stop.",
            router_context,
        )

    return None


def _route_with_runtime(user_text, requested_capability=""):
    # If caller explicitly provides a capability, honor it directly without LLM routing.
    # The LLM sometimes overrides to team-lead even when capability is already known.
    if requested_capability and requested_capability != "null":
        return {
            "workflow": [requested_capability],
            "summary": _truncate_text(user_text or requested_capability, 220),
            "task_type": "dev",
            "office_subtype": None,
            "target_paths": [],
            "needs_input": False,
            "input_question": None,
        }
    system = _build_manifest_prompt(__file__, prompts.ROUTE_SYSTEM)
    prompt = prompts.ROUTE_TEMPLATE.format(
        user_text=user_text or "",
        requested_capability=requested_capability or "null",
    )
    response = _run_agentic(prompt, "route", system_prompt=system)
    data = parse_json_object(response)
    workflow = _normalize_workflow(data.get("workflow") or [])
    if requested_capability and not workflow:
        workflow = [requested_capability]
    if not workflow:
        workflow = ["team-lead.task.analyze"]
    data["workflow"] = workflow
    data["summary"] = _truncate_text(data.get("summary") or user_text, 220)
    data["task_type"] = str(data.get("task_type") or "dev").strip().lower() or "dev"
    data["office_subtype"] = str(data.get("office_subtype") or "").strip().lower() or None
    data["target_paths"] = [
        str(path).strip() for path in (data.get("target_paths") or []) if str(path).strip()
    ]
    data["needs_input"] = bool(data.get("needs_input"))
    data["input_question"] = str(data.get("input_question") or "").strip() or None
    return data


def _summarize_for_user(task, state, status_message, artifacts, workflow):
    user_text = extract_text(task.original_message or {})
    artifact_lines = []
    for artifact in artifacts or []:
        name = artifact.get("name") or "artifact"
        text = artifact_text(artifact) or str(artifact.get("text") or "")
        text = _truncate_text(text, 240)
        if text:
            artifact_lines.append(f"- {name}: {text}")
    prompt = prompts.FINAL_SUMMARY_TEMPLATE.format(
        user_text=user_text or "",
        workflow=", ".join(workflow or []),
        state=state or "",
        status_message=status_message or "",
        artifacts_summary="\n".join(artifact_lines) or "(none)",
    )
    system = _build_manifest_prompt(__file__, prompts.FINAL_SUMMARY_SYSTEM)
    response = _run_agentic(prompt, "final-summary", system_prompt=system)
    data = parse_json_object(response)
    summary = str(data.get("summary") or "").strip()
    return summary or status_message


def _create_shared_workspace(task_id):
    workspace_root = os.path.join(artifact_store.root, "workspaces")
    os.makedirs(workspace_root, exist_ok=True)
    timestamp = local_file_timestamp()
    workspace_path = os.path.join(workspace_root, f"{task_id}-{timestamp}")
    os.makedirs(workspace_path, exist_ok=True)
    return workspace_path


def _read_workspace_json(workspace_path, relative_path):
    if not workspace_path:
        return {}
    full_path = os.path.join(workspace_path, relative_path)
    if not os.path.isfile(full_path):
        return {}
    try:
        with open(full_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _extract_pr_evidence_from_artifacts(artifacts: list) -> dict:
    """Extract PR evidence (URL, branch, jiraInReview) from A2A artifacts.

    Execution agents (android, web) embed prUrl and branch in their artifact
    metadata and send them via A2A callback to Team Lead, which passes them
    through to Compass.  Compass must NOT scan the shared workspace filesystem
    to find these files — all evidence must come through A2A artifacts.
    """
    for artifact in artifacts or []:
        metadata = artifact.get("metadata") or {}
        pr_url = metadata.get("prUrl") or metadata.get("url") or ""
        branch = metadata.get("branch") or ""
        if pr_url:
            return {
                "url": pr_url,
                "branch": branch,
                "jiraInReview": bool(metadata.get("jiraInReview", False)),
            }
    return {}


def _truncate_text(value, limit=180):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _extract_design_reference(text):
    figma = FIGMA_URL_RE.search(text or "")
    if figma:
        return figma.group().rstrip(".,;)\"'"), "figma"
    stitch = STITCH_URL_RE.search(text or "")
    if stitch:
        return stitch.group().rstrip(".,;)\"'"), "stitch"
    return "", ""


def _read_workspace_log_sections(workspace_path, max_lines=40, max_chars=12000):
    if not workspace_path or not os.path.isdir(workspace_path):
        return []

    sections = []
    for entry in os.listdir(workspace_path):
        agent_dir = os.path.join(workspace_path, entry)
        if not os.path.isdir(agent_dir):
            continue
        log_path = os.path.join(agent_dir, "command-log.txt")
        if not os.path.isfile(log_path):
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        content = "".join(lines[-max_lines:]).strip()
        if not content:
            continue
        if len(content) > max_chars:
            content = content[-max_chars:]
        sections.append({
            "agentId": entry,
            "title": entry.replace("-", " ").title(),
            "lineCount": len(lines),
            "updatedAt": os.path.getmtime(log_path),
            "content": content,
        })

    sections.sort(key=lambda section: section["updatedAt"], reverse=True)
    return sections


def _refresh_task_card_metadata(task):
    workspace_path = getattr(task, "workspace_path", "")
    original_text = extract_text(task.original_message or {})

    if not getattr(task, "summary", ""):
        task.summary = _truncate_text(original_text, 180)
    if not getattr(task, "jira_ticket_id", ""):
        ticket_match = TICKET_RE.search(original_text or "")
        task.jira_ticket_id = ticket_match.group(0) if ticket_match else ""
    if not getattr(task, "design_url", ""):
        design_url, design_type = _extract_design_reference(original_text)
        task.design_url = design_url
        task.design_type = design_type

    analysis = {}
    current_phase = ""
    design_context = {}
    jira_context = {}
    plan = {}
    pr_evidence = {}

    if workspace_path:
        stage_summary = _read_workspace_json(workspace_path, "team-lead/stage-summary.json")
        analysis = stage_summary.get("analysis") if isinstance(stage_summary.get("analysis"), dict) else {}
        current_phase = str(stage_summary.get("currentPhase") or "")
        design_context = _read_workspace_json(workspace_path, "team-lead/design-context.json")
        jira_context = _read_workspace_json(workspace_path, "team-lead/jira-context.json")
        plan = _read_workspace_json(workspace_path, "team-lead/plan.json")
        # PR evidence comes from A2A artifacts delivered by the execution agent via Team Lead.
        # We must NOT scan execution-agent subdirectories in the shared workspace — that would
        # bypass the A2A protocol boundary.
        pr_evidence = _extract_pr_evidence_from_artifacts(getattr(task, "artifacts", []))

        task.summary = _truncate_text(
            analysis.get("summary")
            or task.status_message
            or task.summary
            or original_text,
            220,
        )
        task.jira_ticket_id = (
            str(
                analysis.get("jira_ticket_key")
                or jira_context.get("ticket_key")
                or task.jira_ticket_id
                or ""
            )
            .strip()
        )
        task.design_url = str(
            design_context.get("url") or analysis.get("design_url") or task.design_url or ""
        ).strip()
        task.design_type = str(
            design_context.get("type") or analysis.get("design_type") or task.design_type or ""
        ).strip()

    current_major_step = ""
    if task.progress_steps:
        current_major_step = str(task.progress_steps[-1].get("step") or "")
    if not current_major_step:
        current_major_step = current_phase or task.status_message or ""

    return {
        "analysis": analysis,
        "designContext": design_context,
        "jiraContext": jira_context,
        "plan": plan,
        "prEvidence": pr_evidence,
        "currentMajorStep": current_major_step,
        "commandLogSections": _read_workspace_log_sections(workspace_path),
    }


def _task_card_status(task_state, pr_evidence):
    failed_states = {
        "TASK_STATE_FAILED",
        "FAILED",
        "NO_CAPABLE_AGENT",
        "CAPABILITY_TEMPORARILY_UNAVAILABLE",
        "POLICY_DENIED",
        "CAPACITY_EXHAUSTED",
    }
    if task_state == "TASK_STATE_INPUT_REQUIRED":
        return "waiting_for_info", "Waiting for Info"
    if task_state in failed_states:
        return "failed", "Failed"
    if task_state == "TASK_STATE_COMPLETED":
        pr_url = pr_evidence.get("url") or pr_evidence.get("prUrl") or ""
        if pr_url:
            # jiraInReview flag is set by execution agents in their artifact metadata
            # when they successfully transition the Jira ticket to "In Review".
            if pr_evidence.get("jiraInReview"):
                return "completed", "Completed / In Review"
            return "completed", "Completed / PR Raised"
        return "completed", "Completed"
    return "in_progress", "In Progress"


def _serialize_task_card(task):
    metadata = _refresh_task_card_metadata(task)
    pr_evidence = metadata["prEvidence"] if isinstance(metadata["prEvidence"], dict) else {}
    status_kind, status_label = _task_card_status(task.state, pr_evidence)
    design_context = metadata["designContext"] if isinstance(metadata["designContext"], dict) else {}

    steps = []
    for item in task.progress_steps[-8:]:
        steps.append({
            "step": item.get("step") or "",
            "agentId": item.get("agentId") or "",
            "ts": item.get("ts"),
        })

    return {
        "id": task.task_id,
        "contextId": task.context_id,
        "state": task.state,
        "statusMessage": task.status_message,
        "statusKind": status_kind,
        "statusLabel": status_label,
        "summary": task.summary or _truncate_text(extract_text(task.original_message or {}), 220),
        "jiraTicketId": task.jira_ticket_id,
        "design": {
            "url": task.design_url,
            "type": task.design_type,
            "pageName": design_context.get("page_name") or "",
        },
        "workflow": list(task.pending_workflow or []),
        "createdAt": task.created_at,
        "updatedAt": task.updated_at,
        "workspacePath": task.workspace_path,
        "requiresInput": task.state == "TASK_STATE_INPUT_REQUIRED",
        "currentMajorStep": metadata["currentMajorStep"],
        "progressSteps": steps,
        "commandLogSections": metadata["commandLogSections"],
        "pr": {
            "url": pr_evidence.get("url") or pr_evidence.get("prUrl") or "",
            "branch": pr_evidence.get("branch") or "",
        },
    }


def _extract_team_lead_completeness_issues(task, artifacts):
    """Check whether Team Lead's deliverable satisfies completion criteria.

    All evidence is read from the A2A artifacts that Team Lead delivered via
    callback — never from the shared workspace filesystem.  Execution-agent
    workspace files (pr-evidence.json, jira-actions.json, stage-summary.json)
    are internal to the Team Lead ↔ dev-agent pipeline and must not be
    accessed directly by Compass.
    """
    issues = []
    summary_artifact = None
    for artifact in artifacts or []:
        metadata = artifact.get("metadata") or {}
        if metadata.get("capability") == "team-lead.task.analyze":
            summary_artifact = artifact
            break
    summary_meta = (summary_artifact or {}).get("metadata") or {}

    if summary_meta.get("validationCheckpoint"):
        # Intentional pre-dispatch stop — not a completeness failure.
        return issues

    if summary_meta.get("reviewMaxCyclesReached"):
        # Team Lead exhausted review cycles and deliberately accepted the output.
        # Respect that decision and skip the completeness retry.
        print("[compass] Team Lead reached max review cycles and accepted with issues — skipping retry.")
        return issues

    if summary_meta.get("reviewPassed") is False:
        issues.append("Team Lead review did not pass.")

    if summary_meta.get("reviewPassed") is True:
        # Team Lead reviewed and approved — trust the review completely.
        return issues

    # reviewPassed is None: Team Lead did not set the flag (should not happen in
    # normal operation).  Fall back to artifact-based evidence checks only.
    # We check Team Lead's own workspace files for target_repo_url and jira key
    # (those are Team Lead's output, not execution-agent files).
    workspace_path = getattr(task, "workspace_path", "")
    if workspace_path:
        team_lead_plan = _read_workspace_json(workspace_path, "team-lead/plan.json")
        team_lead_stage = _read_workspace_json(workspace_path, "team-lead/stage-summary.json")
        analysis = team_lead_stage.get("analysis") if isinstance(team_lead_stage.get("analysis"), dict) else {}
        target_repo_url = (team_lead_plan.get("target_repo_url") or analysis.get("target_repo_url") or "").strip()

        if target_repo_url:
            # PR evidence must come from A2A artifacts, not from filesystem scanning.
            pr_evidence = _extract_pr_evidence_from_artifacts(artifacts)
            if not (pr_evidence.get("url") or pr_evidence.get("prUrl")):
                issues.append("Pull request URL is missing from execution agent artifacts.")
            if not pr_evidence.get("branch"):
                issues.append("Branch name is missing from execution agent artifacts.")

    return issues


def _build_completeness_follow_up_message(original_message, issues, revision_cycle):
    message = deep_copy_json(original_message)
    base_text = extract_text(message)
    issue_lines = "\n".join(f"- {issue}" for issue in issues)
    follow_up = (
        f"Compass completeness check revision {revision_cycle} found unresolved gaps:\n"
        f"{issue_lines}\n\n"
        "Continue from the existing shared workspace, preserve prior work, and use only registered boundary agents."
    )
    message["parts"] = [{"text": (base_text + "\n\n" + follow_up).strip()}]
    metadata = dict(message.get("metadata") or {})
    metadata["compassCompletenessRevision"] = revision_cycle
    metadata["completenessIssues"] = issues
    message["metadata"] = metadata
    return message


def _read_agent_logs(since=0):
    generated_at = time.time()
    try:
        containers = launcher.list_agent_containers(include_stopped=True)
    except Exception as error:
        return {
            "generatedAt": generated_at,
            "agents": [],
            "error": str(error),
        }

    agents = []
    for container in containers:
        try:
            logs = launcher.read_container_logs(container["container_id"], since=since, tail=200)
        except Exception as error:
            logs = [{"ts": "", "line": f"[log_error] {error}"}]
        agents.append({
            "agentId": container["agent_id"],
            "displayName": container["display_name"],
            "role": container["role"],
            "state": container["state"],
            "status": container["status"],
            "containerId": container["container_id"],
            "containerName": container["container_name"],
            "taskId": container.get("task_id"),
            "logs": logs,
        })
    return {
        "generatedAt": generated_at,
        "agents": agents,
    }


def _a2a_call(agent_url, message, context_id=None):
    body = {
        "message": message,
        "configuration": {
            "returnImmediately": True,
            "acceptedOutputModes": ["text/plain"],
        },
    }
    if context_id:
        body["contextId"] = context_id

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{agent_url}/message:send",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    print(f"[compass] Dispatching to {agent_url}/message:send")
    with urlopen(request, timeout=ACK_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_task(agent_url, task_id):
    request = Request(
        f"{agent_url.rstrip('/')}/tasks/{task_id}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=ACK_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _send_agent_ack(service_url: str, task_id: str) -> None:
    """ACK a per-task agent so it can proceed with its exit rule (best-effort)."""
    if not service_url or not task_id:
        return
    request = Request(
        f"{service_url.rstrip('/')}/tasks/{task_id}/ack",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            pass
        print(f"[compass] ACK sent to {service_url} for task {task_id}")
    except Exception as err:
        print(f"[compass] Could not ACK agent at {service_url} task {task_id}: {err}")


def _extract_requested_capability(body, message):
    top_level = body.get("requestedCapability")
    if top_level:
        return top_level
    metadata = body.get("metadata", {})
    if metadata.get("requestedCapability"):
        return metadata["requestedCapability"]
    message_metadata = message.get("metadata", {})
    return message_metadata.get("requestedCapability")


def _dedupe(items):
    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _is_terminal_state(state):
    return (state or "TASK_STATE_COMPLETED") not in NON_TERMINAL_TASK_STATES


def _run_workflow(task_id, message, workflow):
    task = task_store.get(task_id)
    if not task:
        return
    run_compass_workflow(
        task_id=task_id,
        task=task,
        message=message,
        workflow=workflow,
        agent_id=AGENT_ID,
        agent_file=__file__,
        route_system_prompt=prompts.ROUTE_SYSTEM,
        advertised_url=ADVERTISED_URL,
        compass_instance_id=COMPASS_INSTANCE_ID,
        max_revisions=COMPASS_COMPLETENESS_MAX_REVISIONS,
        timeout_seconds=DOWNSTREAM_TASK_TIMEOUT,
        get_task=task_store.get,
        update_state_and_notify=_update_state_and_notify,
        summarize_for_user=_summarize_for_user,
        add_progress_step=task_store.add_progress_step,
        audit_log=audit_log,
    )


def route_and_dispatch(message, requested_capability=None, forced_workflow=None):
    task = task_store.create()
    task.workspace_path = _create_shared_workspace(task.task_id)
    task.original_message = deep_copy_json(message)
    task.router_context = {}
    user_text = extract_text(message)
    task.summary = _truncate_text(user_text, 180)
    ticket_match = TICKET_RE.search(user_text or "")
    task.jira_ticket_id = ticket_match.group(0) if ticket_match else ""
    design_url, design_type = _extract_design_reference(user_text)
    task.design_url = design_url
    task.design_type = design_type

    try:
        require_agentic_runtime("Compass")
    except RuntimeError as exc:
        failure = str(exc)
        task_store.update_state(task.task_id, "TASK_STATE_FAILED", failure)
        audit_log("TASK_FAILED", task_id=task.task_id, error=failure)
        return task.to_dict()

    # Extract owner / channel metadata from message.metadata (IM Gateway sets these)
    msg_meta = message.get("metadata") or {}
    task.owner_user_id = (msg_meta.get("ownerUserId") or "").strip()
    task.owner_display_name = (msg_meta.get("ownerDisplayName") or "").strip()
    task.tenant_id = (msg_meta.get("tenantId") or "").strip()
    task.source_channel = (msg_meta.get("sourceChannel") or "").strip()

    route_decision = None
    if forced_workflow:
        workflow = list(forced_workflow)
    else:
        route_decision = _route_with_runtime(user_text, requested_capability=requested_capability or "")
        workflow = route_decision.get("workflow") or ([requested_capability] if requested_capability else ["team-lead.task.analyze"])
    task.pending_workflow = list(workflow)
    if route_decision and route_decision.get("summary"):
        task.summary = _truncate_text(route_decision.get("summary"), 180)
    audit_log(
        "TASK_CREATED",
        task_id=task.task_id,
        user_text=user_text[:200],
        workflow=workflow,
    )
    record_workspace_stage(
        task.workspace_path,
        "compass",
        "Created task and workspace",
        task_id=task.task_id,
        extra={
            "requestedCapability": requested_capability or "",
            "workflow": workflow,
            "userText": user_text[:1000],
            "runtimeConfig": _runtime_config_summary(),
        },
    )
    task_store.add_progress_step(
        task.task_id,
        "Task created and queued in Compass.",
        agent_id="compass-agent",
    )
    task_store.add_progress_step(
        task.task_id,
        f"Created shared workspace: {task.workspace_path}",
        agent_id="compass-agent",
    )

    if route_decision and route_decision.get("needs_input"):
        task_type = route_decision.get("task_type") or "development"
        if task_type == "dev":
            task_type = "development"
        return _route_input_required(
            task,
            route_decision.get("input_question") or "Please clarify the request.",
            {
                "kind": route_decision.get("task_type") or "general",
                "awaitingStep": "clarify_path",
                "requestedCapability": workflow[0] if workflow else requested_capability,
                "task_type": task_type,
            },
        )

    # Store task_type in router_context for permission loading during dispatch
    if route_decision:
        task_type = route_decision.get("task_type") or "development"
        if task_type == "dev":
            task_type = "development"
        task.router_context["task_type"] = task_type

    office_response = _maybe_prepare_office_route(task, workflow, route_decision or {})
    if office_response is not None:
        return office_response

    return _start_task_worker(task, message, workflow)


def _is_team_lead_reachable(service_url: str) -> bool:
    """Return True if the Team Lead service is accepting requests."""
    if not service_url:
        return False
    try:
        from urllib.request import urlopen
        from urllib.error import URLError
        with urlopen(f"{service_url.rstrip('/')}/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _resume_input_required_task(body: dict, message: dict) -> dict | None:
    context_id = (body.get("contextId") or message.get("contextId") or "").strip()
    auto_routed = False
    if not context_id:
        # No contextId supplied (e.g. page was refreshed). Auto-detect if there
        # is exactly one outstanding INPUT_REQUIRED task that has a waiting
        # downstream Team Lead — if so, treat this reply as belonging to it.
        all_tasks = task_store.list_tasks()
        pending_input = [
            t for t in all_tasks
            if t.state == "TASK_STATE_INPUT_REQUIRED"
            and getattr(t, "downstream_task_id", None)
        ]
        if len(pending_input) == 1:
            candidate = pending_input[0]
            # Only auto-route if the downstream Team Lead is still reachable.
            candidate_svc_url = getattr(candidate, "downstream_service_url", "") or ""
            if _is_team_lead_reachable(candidate_svc_url):
                context_id = candidate.task_id
                auto_routed = True
                print(
                    f"[compass] Auto-routing reply to single pending INPUT_REQUIRED task: {context_id}"
                )
            else:
                # Team Lead container is gone — mark stale task as failed and let a
                # new task be created for this message.
                print(
                    f"[compass] Stale INPUT_REQUIRED task {candidate.task_id} — "
                    f"downstream service {candidate_svc_url!r} is unreachable; creating new task"
                )
                task_store.update_state(
                    candidate.task_id,
                    "TASK_STATE_FAILED",
                    "Task cancelled: the agent handling this task is no longer running.",
                )
                return None
        else:
            return None

    prior_task = task_store.get(context_id)
    if not prior_task or prior_task.state != "TASK_STATE_INPUT_REQUIRED":
        return None

    routed_task = _resume_compass_routed_task(prior_task, message)
    if routed_task is not None:
        return routed_task

    tl_task_id = prior_task.downstream_task_id or ""
    tl_service_url = prior_task.downstream_service_url or ""

    if tl_task_id and not tl_service_url:
        try:
            # Discover the orchestrator agent dynamically via capability lookup
            # so we never hardcode an agent ID here.
            tl_agents = registry.find_by_capability("team-lead.task.analyze")
            tl_agent_ids = [
                a.get("agent_id") for a in (tl_agents or []) if a.get("agent_id")
            ]
            for tl_agent_id in tl_agent_ids:
                for inst in registry.list_instances(tl_agent_id):
                    if inst.get("current_task_id") == tl_task_id:
                        tl_service_url = inst.get("service_url", "")
                        break
                if tl_service_url:
                    break
            if tl_service_url:
                print(f"[compass] Recovered orchestrator service URL from registry: {tl_service_url}")
        except Exception as lookup_err:
            print(f"[compass] Could not look up orchestrator service URL: {lookup_err}")

    if tl_task_id and tl_service_url:
        print(
            f"[compass] Forwarding user reply to Team Lead "
            f"(tl_task={tl_task_id}, compass_task={context_id})"
        )
        try:
            _a2a_call(tl_service_url, message, context_id=tl_task_id)
            task_store.update_state(
                context_id,
                "TASK_STATE_WORKING",
                "User provided additional information. Resuming…",
            )
            task_store.add_progress_step(
                context_id,
                "User provided additional information. Resuming task.",
                agent_id="compass-agent",
            )
            if getattr(prior_task, "workspace_path", ""):
                record_workspace_stage(
                    prior_task.workspace_path,
                    "compass",
                    "Received user input and resumed task",
                    task_id=context_id,
                    extra={
                        "teamLeadTaskId": tl_task_id,
                        "userText": extract_text(message)[:1000],
                        "runtimeConfig": _runtime_config_summary(),
                    },
                )
            audit_log(
                "TASK_RESUMED",
                task_id=context_id,
                tl_task_id=tl_task_id,
            )
        except Exception as err:
            print(f"[compass] Failed to forward resume to Team Lead: {err}")
            task_store.update_state(
                context_id,
                "TASK_STATE_INPUT_REQUIRED",
                prior_task.status_message,
            )
        return prior_task.to_dict()

    orig_text = extract_text(prior_task.original_message or {})
    new_text = extract_text(message)
    combined_text = (orig_text + "\n\n" + new_text).strip() if orig_text else new_text
    merged = deep_copy_json(message)
    merged["parts"] = [{"text": combined_text}]
    workflow = prior_task.pending_workflow
    print(f"[compass] INPUT_REQUIRED fallback: re-running workflow for task {context_id}")
    return route_and_dispatch(merged, forced_workflow=workflow)


class CompassHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _check_api_key(self):
        """Return True if API key is valid or not configured. Send 401 and return False otherwise."""
        if not COMPASS_API_KEY:
            return True
        auth = (self.headers.get("Authorization") or "").strip()
        if auth == f"Bearer {COMPASS_API_KEY}":
            return True
        # Allow local Web UI requests without auth (no Authorization header at all)
        if not auth:
            return True
        self._send_json(401, {"error": "invalid_api_key"})
        return False

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            try:
                with open(UI_PATH, "r", encoding="utf-8") as handle:
                    self._send_html(200, handle.read())
            except OSError as error:
                self._send_json(500, {"error": "ui_unavailable", "message": str(error)})
            return

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": "compass"})
            return

        if path == "/.well-known/agent-card.json":
            card_path = os.path.join(os.path.dirname(__file__), "agent-card.json")
            with open(card_path, encoding="utf-8") as fh:
                card = json.load(fh)
            text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
            self._send_json(200, json.loads(text))
            return

        if path == "/debug/agent-logs":
            query = parse_qs(urlparse(self.path).query)
            try:
                since = int(float(query.get("since", [0])[0]))
            except (TypeError, ValueError):
                since = 0
            self._send_json(200, _read_agent_logs(since=since))
            return

        if path == "/api/notification-targets":
            with _notification_targets_lock:
                targets = list(_notification_targets)
            self._send_json(200, {"targets": targets})
            return

        if path == "/api/tasks":
            query = parse_qs(urlparse(self.path).query)
            owner_filter = ((query.get("ownerUserId") or [""])[0] or "").strip()
            all_tasks = task_store.list_tasks(owner_filter or None)
            cards = [_serialize_task_card(task) for task in all_tasks]
            self._send_json(200, {"tasks": cards})
            return

        m = re.fullmatch(r"/api/tasks/([^/]+)/card", path)
        if m:
            task = task_store.get(m.group(1))
            if not task:
                self._send_json(404, {"error": "task_not_found"})
                return
            self._send_json(200, {"task": _serialize_task_card(task)})
            return

        if path.startswith("/tasks/"):
            suffix = path.split("/tasks/", 1)[1]
            if suffix.endswith("/artifacts"):
                task_id = suffix[:-len("/artifacts")]
                artifacts = artifact_store.get_by_task(task_id)
                self._send_json(200, {
                    "taskId": task_id,
                    "artifacts": [artifact.to_dict(include_content=True) for artifact in artifacts],
                })
                return

            task = task_store.get(suffix)
            if task:
                self._send_json(200, {"task": task.to_dict()})
            else:
                self._send_json(404, {"error": "task_not_found"})
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)

        # POST /api/notification-targets — register a webhook URL for task state notifications
        if path == "/api/notification-targets":
            if not self._check_api_key():
                return
            body = self._read_body()
            url = (body.get("url") or "").strip()
            if not url:
                self._send_json(400, {"error": "missing_url"})
                return
            with _notification_targets_lock:
                existing = [t for t in _notification_targets if t["url"] == url]
                if not existing:
                    _notification_targets.append({"url": url, "registeredAt": local_iso_timestamp()})
                    print(f"[compass] Registered notification target: {url}")
            self._send_json(200, {"ok": True})
            return

        # POST /tasks/{task_id}/progress — agents report major workflow steps
        m = re.fullmatch(r"/tasks/([^/]+)/progress", path)
        if m:
            task_id = m.group(1)
            # Reject progress reports from stale Compass instances
            caller_instance = (qs.get("instance") or [None])[0]
            if caller_instance and caller_instance != COMPASS_INSTANCE_ID:
                print(f"[compass] Stale progress ignored (task={task_id}, instance={caller_instance})")
                self._send_json(410, {"error": "stale_instance"})
                return
            body = self._read_body()
            step = (body.get("step") or "").strip()
            agent_id = body.get("agentId", "")
            ts = body.get("ts")
            if step:
                task_store.add_progress_step(task_id, step, agent_id=agent_id, ts=ts)
                print(f"[compass] Progress [{task_id}] <{agent_id}>: {step}")
                task = task_store.get(task_id)
                if task and getattr(task, "workspace_path", ""):
                    record_workspace_stage(
                        task.workspace_path,
                        "compass",
                        step,
                        task_id=task_id,
                        extra={"sourceAgent": agent_id, "runtimeConfig": _runtime_config_summary()},
                    )
            self._send_json(200, {"ok": True})
            return

        # POST /tasks/{task_id}/callbacks — downstream agents notify completion
        m = re.fullmatch(r"/tasks/([^/]+)/callbacks", path)
        if m:
            task_id = m.group(1)
            # Reject callbacks from stale Compass instances
            caller_instance = (qs.get("instance") or [None])[0]
            if caller_instance and caller_instance != COMPASS_INSTANCE_ID:
                print(f"[compass] Stale callback ignored (task={task_id}, instance={caller_instance})")
                self._send_json(410, {"error": "stale_instance"})
                return
            body = self._read_body()
            downstream_task_id = (body.get("downstreamTaskId") or body.get("taskId") or "").strip()
            if not downstream_task_id:
                self._send_json(400, {"error": "missing_downstream_task_id"})
                return
            payload = {
                "state": body.get("state", "TASK_STATE_COMPLETED"),
                "status_message": body.get("statusMessage", ""),
                "artifacts": body.get("artifacts") or [],
                "agent_id": body.get("agentId", ""),
            }
            audit_log(
                "TASK_CALLBACK_RECEIVED",
                task_id=task_id,
                downstream_task_id=downstream_task_id,
                agent_id=payload["agent_id"],
                state=payload["state"],
            )
            self._send_json(200, {"ok": True})
            return

        if path != "/message:send":
            self._send_json(404, {"error": "not_found"})
            return

        body = self._read_body()
        message = body.get("message", {})
        requested_capability = _extract_requested_capability(body, message)
        if not message:
            self._send_json(400, {"error": "missing message"})
            return

        resumed_task = _resume_input_required_task(body, message)
        if resumed_task is not None:
            self._send_json(200, {"task": resumed_task})
            return

        print(f"[compass] Received message: {json.dumps(message, ensure_ascii=False)[:200]}")
        task_dict = route_and_dispatch(message, requested_capability=requested_capability)
        self._send_json(200, {"task": task_dict})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path == "/api/notification-targets":
            if not self._check_api_key():
                return
            body = self._read_body()
            url = (body.get("url") or "").strip()
            if not url:
                self._send_json(400, {"error": "missing_url"})
                return
            with _notification_targets_lock:
                before = len(_notification_targets)
                _notification_targets[:] = [t for t in _notification_targets if t["url"] != url]
                removed = before - len(_notification_targets)
            if removed:
                print(f"[compass] Unregistered notification target: {url}")
            self._send_json(200, {"ok": True, "removed": removed})
            return
        self._send_json(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        # Suppress noisy health-check, agent-card polls, and debug log polling
        line = args[0] if args else ""
        if any(p in line for p in (
            "/health",
            "/.well-known/agent-card.json",
            "/debug/agent-logs",
            "/api/tasks",
        )):
            return
        print(f"[compass] {line} {args[1] if len(args) > 1 else ''} {args[2] if len(args) > 2 else ''}")


def main():
    print(f"[compass] Compass agent starting on {HOST}:{PORT}")
    print(f"[compass] Instance ID: {COMPASS_INSTANCE_ID}")
    print(f"[compass] Artifact root: {artifact_store.root}")
    reporter = InstanceReporter(agent_id=AGENT_ID, service_url=ADVERTISED_URL, port=PORT)
    reporter.start()
    server = ThreadingHTTPServer((HOST, PORT), CompassHandler)
    # Increase listen backlog so concurrent callback + test requests are not refused.
    # The default (5) causes transient ECONNREFUSED when multiple agents send callbacks
    # simultaneously with new test requests.
    server.socket.listen(128)
    server.serve_forever()


if __name__ == "__main__":
    main()