"""Web Agent — full-stack web development execution agent.

Capabilities:
- Frontend: React, Next.js, Vue.js with Ant Design, Material UI, Tailwind CSS
- Backend: Python (Flask, FastAPI, Django), Node.js (Express, NestJS)
- Analyzes task, generates code, writes files to shared workspace
- Creates feature branch and pull request via SCM Agent
- Can query Jira Agent for additional ticket context
- Reports completion via callback to Team Lead Agent
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from common.agent_directory import (
    AgentDirectory,
    CapabilityUnavailableError,
    RegistryUnavailableError,
)
from common.env_utils import build_isolated_git_env, load_dotenv
from common.instance_reporter import InstanceReporter
from common.message_utils import artifact_text, build_text_artifact, extract_text
from common.per_task_exit import PerTaskExitHandler
from common.registry_client import RegistryClient
from common.rules_loader import build_system_prompt, load_rules
from common.runtime.adapter import get_runtime, summarize_runtime_configuration
from common.task_permissions import (
    PermissionEscalationRequired,
    build_permission_denied_artifact,
    extract_permission_denial,
)
from common.task_store import TaskStore
from common.time_utils import local_clock_time, local_iso_timestamp
from web import prompts

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8050"))
AGENT_ID = os.environ.get("AGENT_ID", "web-agent")
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{AGENT_ID}-local")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://web-agent:{PORT}")
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
COMPASS_URL = os.environ.get("COMPASS_URL", "http://compass:8080")

ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))
TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "600"))
SYNC_AGENT_TIMEOUT = int(os.environ.get("SYNC_AGENT_TIMEOUT_SECONDS", "120"))
PLAN_TIMEOUT_SECONDS = 300
PLAN_REPAIR_TIMEOUT_SECONDS = 120
PLAN_MAX_TOKENS = 8192

_AGENT_CARD_PATH = os.path.join(os.path.dirname(__file__), "agent-card.json")

registry_client = RegistryClient(REGISTRY_URL)
agent_directory = AgentDirectory(AGENT_ID, registry_client)
exit_handler = PerTaskExitHandler()
task_store = TaskStore()
reporter = InstanceReporter(
    agent_id=AGENT_ID,
    service_url=ADVERTISED_URL,
    port=PORT,
)

_DEVELOPMENT_SKILL_NAMES = [
    "constellation-architecture-delivery",
    "constellation-frontend-delivery",
    "constellation-backend-delivery",
    "constellation-database-delivery",
    "constellation-code-review-delivery",
    "constellation-testing-delivery",
    "constellation-ui-evidence-delivery",
    "react-nextjs-delivery",
    "ant-design-delivery",
    "mui-delivery",
    "nodejs-express-delivery",
    "java-spring-delivery",
    "sql-mongodb-delivery",
]

# Viewports for UI implementation screenshots captured after each build.
# Each entry is (width_px, height_px).  Files land in docs/evidence/ named
# screenshot-{W}x{H}.png — no platform labels so the web agent stays generic.
_UI_SCREENSHOT_VIEWPORTS: list[tuple[int, int]] = [
    (1280, 900),   # standard laptop / desktop
    (375, 812),    # standard phone (iPhone-class)
]


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def audit_log(event: str, **kwargs):
    entry = {"ts": local_iso_timestamp(), "event": event, **kwargs}
    print(f"[audit] {json.dumps(entry, ensure_ascii=False)}")


def _report_progress(compass_url: str, compass_task_id: str, step: str):
    """POST progress step to Compass (best-effort)."""
    if not compass_url or not compass_task_id:
        return
    payload = {"step": step, "agentId": AGENT_ID}
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{compass_url.rstrip('/')}/tasks/{compass_task_id}/progress",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5):
            pass
    except Exception as err:
        print(f"[{AGENT_ID}] Progress report failed (non-critical): {err}")


def _save_workspace_file(workspace_path: str, relative_name: str, content: str) -> None:
    """Write content to a file inside the shared workspace (best-effort)."""
    if not workspace_path:
        return
    try:
        full_path = os.path.join(workspace_path, relative_name)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        print(f"[{AGENT_ID}] Warning: could not save workspace file {relative_name}: {exc}")


def _append_workspace_file(workspace_path: str, relative_name: str, content: str) -> None:
    """Append content to a file inside the shared workspace (best-effort)."""
    if not workspace_path:
        return
    try:
        full_path = os.path.join(workspace_path, relative_name)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "a", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        print(f"[{AGENT_ID}] Warning: could not append workspace file {relative_name}: {exc}")


def _read_workspace_json(workspace_path: str, relative_name: str) -> dict:
    """Read a JSON file from the shared workspace (best-effort)."""
    if not workspace_path:
        return {}
    full_path = os.path.join(workspace_path, relative_name)
    if not os.path.isfile(full_path):
        return {}
    try:
        with open(full_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[{AGENT_ID}] Warning: could not read workspace JSON {relative_name}: {exc}")
        return {}


def _append_workspace_event(workspace_path: str, relative_name: str, event: dict) -> None:
    """Append an event object to a workspace JSON file under the `events` key."""
    payload = _read_workspace_json(workspace_path, relative_name)
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    events.append(event)
    payload["events"] = events
    _save_workspace_file(
        workspace_path,
        relative_name,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def _is_path_within(root_path: str, candidate_path: str) -> bool:
    """Return True when *candidate_path* resolves under *root_path*."""
    if not root_path or not candidate_path:
        return False
    try:
        root_real = os.path.realpath(root_path)
        candidate_real = os.path.realpath(candidate_path)
        return os.path.commonpath([root_real, candidate_real]) == root_real
    except (OSError, ValueError):
        return False


def _require_shared_workspace_for_repo_task(repo_url: str, workspace: str) -> None:
    """Repo-backed tasks must use the shared workspace so SCM can clone into it."""
    if repo_url and not workspace:
        raise RuntimeError(
            "Shared workspace path is required for repo-backed development tasks so the SCM agent "
            "can clone the target repository into the task workspace."
        )


def _ensure_clone_path_in_workspace(workspace: str, clone_path: str) -> None:
    """Reject clone paths that do not resolve under the shared workspace."""
    if workspace and clone_path and not _is_path_within(workspace, clone_path):
        raise RuntimeError(
            f"Repository clone path must stay inside the shared workspace. workspace={workspace} clone={clone_path}"
        )


def _resolve_agent_service_url(capability: str) -> str:
    try:
        _, instance = agent_directory.resolve_capability(capability)
    except RegistryUnavailableError as err:
        raise RuntimeError(
            f"Registry unavailable while resolving required capability '{capability}': {err}"
        ) from err
    except CapabilityUnavailableError as err:
        raise RuntimeError(
            f"Required capability '{capability}' is unavailable. "
            "Boundary systems must be accessed only through registered agents."
        ) from err

    service_url = (instance or {}).get("service_url", "")
    if not service_url:
        raise RuntimeError(
            f"Capability '{capability}' is registered but has no routable service URL."
        )
    return service_url.rstrip("/")


def _adf_text_node(text: str, *, href: str = "") -> dict:
    node = {"type": "text", "text": str(text or "")}
    if href:
        node["marks"] = [{"type": "link", "attrs": {"href": href}}]
    return node


def _adf_document(paragraphs: list[list[dict]]) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": paragraph or [_adf_text_node("")],
            }
            for paragraph in paragraphs
        ],
    }


def _adf_plain_text(adf_body: dict | None) -> str:
    if not isinstance(adf_body, dict):
        return ""
    lines = []
    for block in adf_body.get("content", []):
        if not isinstance(block, dict):
            continue
        parts = []
        for inline in block.get("content", []):
            if isinstance(inline, dict) and inline.get("type") == "text":
                parts.append(str(inline.get("text", "")))
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _build_pr_jira_comment_adf(
    pr_url: str,
    branch_name: str,
    test_status: str,
    generated_files: list[dict],
    summary: str,
) -> dict:
    file_paths = [item.get("path", "") for item in generated_files if item.get("path")]
    preview = ", ".join(file_paths[:5])
    if len(file_paths) > 5:
        preview += "…"
    return _adf_document(
        [
            [_adf_text_node("Web Agent completed implementation.")],
            [_adf_text_node("PR: "), _adf_text_node(pr_url, href=pr_url)],
            [_adf_text_node(f"Branch: {branch_name}")],
            [_adf_text_node(f"Test Status: {test_status}")],
            [_adf_text_node(f"Files changed ({len(file_paths)}): {preview}")],
            [_adf_text_node(f"Summary: {summary}")],
        ]
    )


def _record_jira_action(
    workspace_path: str,
    task_id: str,
    ticket_key: str,
    action: str,
    status: str,
    agent_task_id: str = "",
    **details,
) -> None:
    """Persist Jira workflow evidence for later review."""
    event = {
        "ts": local_iso_timestamp(),
        "taskId": task_id,
        "agentTaskId": agent_task_id or task_id,
        "agentId": AGENT_ID,
        "ticketKey": ticket_key,
        "action": action,
        "status": status,
    }
    event.update({key: value for key, value in details.items() if value not in (None, "")})
    _append_workspace_event(workspace_path, f"{AGENT_ID}/jira-actions.json", event)


def _save_pr_evidence(workspace_path: str, **details) -> None:
    """Persist PR metadata and description so review can verify SCM evidence locally."""
    payload = _read_workspace_json(workspace_path, f"{AGENT_ID}/pr-evidence.json")
    payload.update({key: value for key, value in details.items() if value is not None})
    payload.setdefault("agentId", AGENT_ID)
    payload.setdefault("ts", local_iso_timestamp())
    _save_workspace_file(
        workspace_path,
        f"{AGENT_ID}/pr-evidence.json",
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def _prepend_tech_stack_constraints(task_instruction: str, constraints: dict | None) -> str:
    if not constraints:
        return task_instruction
    lines = ["HARD TECH STACK CONSTRAINTS:"]
    if constraints.get("language") == "python":
        version = constraints.get("python_version")
        lines.append(f"- Use Python{f' {version}' if version else ''}.")
    if constraints.get("backend_framework"):
        lines.append(f"- Use {constraints['backend_framework']} for the backend/web server.")
    if constraints.get("frontend_framework"):
        lines.append(f"- Use {constraints['frontend_framework']} for the frontend.")
    lines.append("- Do not switch to React, Next.js, or Node.js unless the user explicitly overrides these constraints.")
    lines.append("- If the target repo is empty or sparse, scaffold the required stack in-place.")
    block = "\n".join(lines)
    if block in task_instruction:
        return task_instruction
    return f"{block}\n\n{task_instruction}".strip()


def _apply_tech_stack_constraints(analysis: dict, constraints: dict | None) -> dict:
    if not constraints:
        return analysis
    updated = dict(analysis or {})
    if constraints.get("language"):
        updated["language"] = constraints["language"]
    if constraints.get("backend_framework"):
        updated["backend_framework"] = constraints["backend_framework"]
        if updated.get("scope") in (None, "", "frontend_only"):
            updated["scope"] = "fullstack"
    if constraints.get("frontend_framework"):
        updated["frontend_framework"] = constraints["frontend_framework"]
    elif constraints.get("backend_framework") == "flask":
        updated["frontend_framework"] = "none"
        updated.setdefault("ui_library", "none")
    return updated


def _jira_request_json(
    capability: str,
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    permissions: dict | None = None,
    workspace: str = "",
    task_id: str = "",
    compass_task_id: str = "",
    timeout: int = 30,
) -> dict:
    request_payload = dict(payload) if isinstance(payload, dict) else {}
    normalized_path = urlparse(path).path
    ticket_match = re.search(r"/jira/(?:tickets|transitions|comments|assignee)/([A-Z][A-Z0-9]+-\d+)", normalized_path)
    ticket_key = ticket_match.group(1) if ticket_match else ""
    extra_metadata: dict = {}
    if ticket_key:
        extra_metadata["ticketKey"] = ticket_key

    if capability == "jira.ticket.fetch":
        message_text = f"Fetch Jira ticket {ticket_key}".strip()
    elif capability == "jira.user.myself":
        message_text = "Get current Jira user"
    elif capability == "jira.ticket.transition":
        transition = str(request_payload.get("transition") or "").strip()
        if transition:
            extra_metadata["transition"] = transition
        message_text = f"Transition ticket {ticket_key} to {transition}".strip()
    elif capability == "jira.ticket.assignee":
        account_id = str(request_payload.get("accountId") or "").strip()
        if account_id:
            extra_metadata["accountId"] = account_id
        message_text = f"Assign ticket {ticket_key} to accountId {account_id}".strip()
    elif capability == "jira.comment.add":
        adf_body = request_payload.get("adf") if isinstance(request_payload.get("adf"), dict) else None
        if adf_body:
            extra_metadata["adf"] = adf_body
            message_text = f"Add structured comment to ticket {ticket_key}".strip()
        else:
            comment_text = str(request_payload.get("text") or request_payload.get("comment") or "").strip()
            if comment_text:
                extra_metadata["commentText"] = comment_text
            message_text = f"Add comment to ticket {ticket_key}: {comment_text}".strip()
    else:
        raise RuntimeError(f"Unsupported Jira A2A capability: {capability}")

    result = _call_sync_agent(
        capability,
        message_text,
        task_id,
        workspace,
        compass_task_id or task_id,
        permissions=permissions,
        extra_metadata=extra_metadata,
    )
    state = str((result.get("status") or {}).get("state") or "").strip()
    if state in {"TASK_STATE_FAILED", "FAILED"}:
        status_text = extract_text((result.get("status") or {}).get("message") or {}).strip()
        raise RuntimeError(status_text or f"{capability} failed")

    for artifact in result.get("artifacts", []):
        text = artifact_text(artifact)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if capability == "jira.ticket.fetch":
            if artifact.get("name") == "jira-raw-payload":
                return {"issue": data}
            if isinstance(data, dict) and "issue" in data:
                return data
            continue
        if isinstance(data, dict):
            return data
    return {"issue": {}} if capability == "jira.ticket.fetch" else {}


def _get_jira_account_id(
    workspace: str,
    task_id: str,
    permissions: dict | None = None,
    compass_task_id: str = "",
) -> str:
    response = _jira_request_json(
        "jira.user.myself",
        "GET",
        "/jira/myself",
        permissions=permissions,
        workspace=workspace,
        task_id=task_id,
        compass_task_id=compass_task_id,
    )
    user = response.get("user") or {}
    account_id = user.get("accountId") or ""
    if not account_id:
        raise RuntimeError(f"jira.myself returned no accountId: {response}")
    return account_id


def _notify_callback(
    callback_url: str,
    task_id: str,
    state: str,
    status_message: str,
    artifacts: list | None = None,
):
    """Notify Team Lead Agent (or caller) of task completion."""
    if not callback_url:
        return
    payload = {
        "downstreamTaskId": task_id,
        "state": state,
        "statusMessage": status_message,
        "artifacts": artifacts or [],
        "agentId": AGENT_ID,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        callback_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            pass
        print(f"[{AGENT_ID}] Callback sent: task={task_id} state={state}")
    except Exception as err:
        print(f"[{AGENT_ID}] Callback failed: {err}")


def _auto_stop_after_task_enabled() -> bool:
    return os.environ.get("AUTO_STOP_AFTER_TASK", "").strip() == "1"


def _apply_task_exit_rule(task_id: str, exit_rule: dict) -> None:
    """Apply the exit rule for a completed task in a background thread."""
    def _run():
        rule_type = (exit_rule or {}).get("type", "wait_for_parent_ack")
        # If AUTO_STOP env is not set and rule is "auto_stop", treat as persistent
        if rule_type == "auto_stop":
            if not _auto_stop_after_task_enabled():
                print(f"[{AGENT_ID}] AUTO_STOP_AFTER_TASK not set — keeping agent alive")
                return
            rule_type = "immediate"
        exit_handler.apply(
            task_id,
            {**exit_rule, "type": rule_type},
            shutdown_fn=_schedule_shutdown,
            agent_id=AGENT_ID,
        )
    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# A2A helpers for calling downstream agents
# ---------------------------------------------------------------------------

def _a2a_send(agent_url: str, message: dict) -> dict:
    """Send a message to an agent and return the downstream task dict."""
    body = {
        "message": message,
        "configuration": {"returnImmediately": True},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{agent_url.rstrip('/')}/message:send",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=ACK_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8")).get("task", {})


def _poll_task(agent_url: str, task_id: str, timeout: int = 60) -> dict | None:
    """Poll GET /tasks/{id} until a terminal state is reached."""
    terminal = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            request = Request(
                f"{agent_url.rstrip('/')}/tasks/{task_id}",
                headers={"Accept": "application/json"},
            )
            with urlopen(request, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                task = data.get("task", {})
                state = task.get("status", {}).get("state", "")
                if state in terminal:
                    return task
        except Exception:
            pass
        time.sleep(3)
    return None


def _call_sync_agent(
    capability: str,
    message_text: str,
    task_id: str,
    workspace_path: str,
    compass_task_id: str,
    permissions: dict | None = None,
    extra_metadata: dict | None = None,
) -> dict:
    """Call a synchronous agent and wait for its result."""
    agent_url = _resolve_agent_service_url(capability)
    message = {
        "messageId": f"web-{task_id}-{capability}-{int(time.time())}",
        "role": "ROLE_USER",
        "parts": [{"text": message_text}],
        "metadata": {
            "requestedCapability": capability,
            "orchestratorTaskId": compass_task_id,
            "sharedWorkspacePath": workspace_path,
        },
    }
    if isinstance(permissions, dict) and permissions:
        message["metadata"]["permissions"] = permissions
    if isinstance(extra_metadata, dict) and extra_metadata:
        message["metadata"].update(extra_metadata)
    downstream = _a2a_send(agent_url, message)
    task_id_ds = downstream.get("id", "")
    state = downstream.get("status", {}).get("state", "")

    terminal = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED"}
    if state in terminal:
        details = extract_permission_denial(downstream)
        if state in {"TASK_STATE_FAILED", "FAILED"} and details is not None:
            raise PermissionEscalationRequired(details)
        return downstream

    if task_id_ds:
        result = _poll_task(agent_url, task_id_ds, timeout=SYNC_AGENT_TIMEOUT)
        if result:
            details = extract_permission_denial(result)
            result_state = result.get("status", {}).get("state", "")
            if result_state in {"TASK_STATE_FAILED", "FAILED"} and details is not None:
                raise PermissionEscalationRequired(details)
            return result

    return downstream


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _parse_json_from_llm(text: str) -> dict:
    """Extract JSON object from LLM response, stripping markdown fences."""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        start = 1
        end = len(lines)
        while end > start and lines[end - 1].strip() in ("```", ""):
            end -= 1
        text = "\n".join(lines[start:end]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    print(f"[{AGENT_ID}] Warning: could not parse JSON from LLM: {text[:200]}")
    return {}


def _run_agentic(
    prompt: str,
    actor: str,
    *,
    system_prompt: str | None = None,
    context: dict | None = None,
    model: str | None = None,
    timeout: int = 120,
    max_tokens: int = 4096,
) -> str:
    """Run the configured runtime and return raw output text."""
    result = get_runtime().run(
        prompt=prompt,
        context=context,
        system_prompt=system_prompt,
        model=model,
        timeout=timeout,
        max_tokens=max_tokens,
    )
    for warning in result.get("warnings") or []:
        print(f"[{AGENT_ID}] Runtime warning ({actor}): {warning}")
    return result.get("raw_response") or result.get("summary") or ""


def _build_web_system_prompt(base_prompt: str) -> str:
    return build_system_prompt(
        base_prompt,
        "web",
        skill_names=_DEVELOPMENT_SKILL_NAMES,
    )


def _analyze_task(task_instruction: str, acceptance_criteria: list, repo_context: str) -> dict:
    criteria_text = "\n".join(f"- {c}" for c in (acceptance_criteria or [])) or "Not specified."
    prompt = prompts.ANALYZE_TEMPLATE.format(
        task_instruction=task_instruction,
        acceptance_criteria=criteria_text,
        repo_context=repo_context or "None provided.",
    )
    system = _build_web_system_prompt(prompts.ANALYZE_SYSTEM)
    response = _run_agentic(prompt, f"[{AGENT_ID}] analyze", system_prompt=system)
    return _parse_json_from_llm(response)


def _plan_implementation(
    task_instruction: str,
    acceptance_criteria: list,
    analysis: dict,
    repo_snapshot: str,
    design_context: str,
) -> dict:
    criteria_text = "\n".join(f"- {c}" for c in (acceptance_criteria or [])) or "Not specified."
    analysis_json = json.dumps(analysis, ensure_ascii=False, indent=2)
    repo_snapshot_text = repo_snapshot or "No existing codebase."
    design_context_text = design_context or "No design context provided."
    prompt = prompts.PLAN_TEMPLATE.format(
        task_instruction=task_instruction,
        acceptance_criteria=criteria_text,
        analysis_json=analysis_json,
        repo_snapshot=repo_snapshot_text,
        design_context=design_context_text,
    )
    system = _build_web_system_prompt(prompts.PLAN_SYSTEM)
    response = _run_agentic(
        prompt,
        f"[{AGENT_ID}] plan",
        system_prompt=system,
        timeout=PLAN_TIMEOUT_SECONDS,
        max_tokens=PLAN_MAX_TOKENS,
    )
    plan = _parse_json_from_llm(response)
    if plan.get("files"):
        return plan

    repair_prompt = prompts.PLAN_REPAIR_TEMPLATE.format(
        task_instruction=task_instruction,
        acceptance_criteria=criteria_text,
        analysis_json=analysis_json,
        repo_snapshot=repo_snapshot_text,
        design_context=design_context_text,
        previous_response=response or "<empty response>",
    )
    repair_system = _build_web_system_prompt(prompts.PLAN_REPAIR_SYSTEM)
    repaired_response = _run_agentic(
        repair_prompt,
        f"[{AGENT_ID}] plan-repair",
        system_prompt=repair_system,
        timeout=PLAN_REPAIR_TIMEOUT_SECONDS,
        max_tokens=PLAN_MAX_TOKENS,
    )
    repaired_plan = _parse_json_from_llm(repaired_response)
    return repaired_plan or plan


def _self_assess_implementation(
    task_instruction: str,
    acceptance_criteria: list,
    generated_files: list[dict],
    build_ok: bool | None,
    build_output: str,
    screenshot_paths: list[str],
) -> dict:
    """Ask LLM to self-review the implementation vs acceptance criteria.

    Returns {"passed": bool, "issues": [...], "files_to_fix": [...], "summary": "..."}
    """
    criteria_text = "\n".join(f"- {c}" for c in (acceptance_criteria or [])) or "Not specified."
    files_summary = "\n".join(
        f"- {gf['path']} ({gf.get('action', 'create')})"
        for gf in generated_files
    ) or "No files generated."
    if build_ok is None:
        test_results = "Build/test was not run."
    elif build_ok:
        test_results = "✅ Build and tests passed."
    else:
        excerpt = (build_output or "")[:1000]
        test_results = f"❌ Build/tests failed.\n{excerpt}"
    if screenshot_paths:
        screenshot_hint = f"Screenshots captured: {', '.join(os.path.basename(p) for p in screenshot_paths)}"
    else:
        screenshot_hint = "No screenshots captured (non-UI task or screenshot failed)."
    prompt = prompts.SELF_ASSESS_TEMPLATE.format(
        task_instruction=task_instruction[:2000],
        acceptance_criteria=criteria_text,
        files_summary=files_summary,
        test_results=test_results,
        screenshot_hint=screenshot_hint,
    )
    system = _build_web_system_prompt(prompts.SELF_ASSESS_SYSTEM)
    response = _run_agentic(prompt, f"[{AGENT_ID}] self-assess", system_prompt=system, timeout=60)
    result = _parse_json_from_llm(response)
    if not isinstance(result, dict):
        return {"passed": False, "issues": ["Assessment parse error"], "files_to_fix": [], "summary": ""}
    return result


def _compare_design_fidelity(
    generated_files: list[dict],
    build_ok: bool | None,
    build_output: str,
    design_spec: str,
    reference_html: str,
    screenshot_paths: list[str],
) -> dict:
    """Ask LLM to compare the implementation against the design source of truth."""
    if not design_spec and not reference_html:
        return {
            "fidelity_score": 100,
            "implemented": [],
            "missing": [],
            "redundant": [],
            "wrong": [],
            "summary": "No design reference provided.",
        }

    implemented_sections: list[str] = []
    for generated_file in generated_files:
        path = generated_file.get("path", "")
        if not path.lower().endswith((".js", ".jsx", ".ts", ".tsx", ".css", ".html")):
            continue
        content = (generated_file.get("content") or "")[:4000]
        implemented_sections.append(f"## {path}\n{content}")
    implemented_files = "\n\n".join(implemented_sections) or "No relevant UI files generated."

    if build_ok is None:
        build_status = "Build/test was not run."
    elif build_ok:
        build_status = "✅ Build and tests passed."
    else:
        build_status = f"❌ Build/tests failed.\n{(build_output or '')[:1000]}"
    if screenshot_paths:
        build_status += f"\nScreenshots: {', '.join(os.path.basename(path) for path in screenshot_paths)}"

    prompt = prompts.DESIGN_COMPARE_TEMPLATE.format(
        design_spec=(design_spec or "No design spec provided.")[:12000],
        reference_html=(reference_html or "No reference HTML provided.")[:12000],
        implemented_files=implemented_files[:20000],
        build_status=build_status,
    )
    system = _build_web_system_prompt(prompts.DESIGN_COMPARE_SYSTEM)
    response = _run_agentic(prompt, f"[{AGENT_ID}] design-compare", system_prompt=system, timeout=90)
    result = _parse_json_from_llm(response)
    if not isinstance(result, dict):
        return {
            "fidelity_score": 0,
            "implemented": [],
            "missing": [
                {
                    "requirement": "Design comparison parse error",
                    "severity": "major",
                    "file_to_fix": "",
                    "fix_hint": "Re-run the design audit with a valid JSON response.",
                }
            ],
            "redundant": [],
            "wrong": [],
            "summary": "Design comparison parse error.",
        }
    result.setdefault("implemented", [])
    result.setdefault("missing", [])
    result.setdefault("redundant", [])
    result.setdefault("wrong", [])
    result.setdefault("summary", "")
    return result


def _generate_file_code(
    file_info: dict,
    task_instruction: str,
    analysis: dict,
    context_from_files: str,
    existing_content: str = "",
) -> str:
    """Generate source code for a single file."""
    prompt = prompts.CODEGEN_TEMPLATE.format(
        file_path=file_info.get("path", "unknown"),
        action=file_info.get("action", "create"),
        purpose=file_info.get("purpose", ""),
        key_logic=file_info.get("key_logic", ""),
        dependencies=", ".join(file_info.get("dependencies", [])) or "standard library",
        task_instruction=task_instruction,
        frontend_framework=analysis.get("frontend_framework", "none"),
        ui_library=analysis.get("ui_library", "none"),
        backend_framework=analysis.get("backend_framework", "none"),
        language=analysis.get("language", "javascript"),
        existing_content=existing_content or "N/A (new file)",
        context_from_other_files=context_from_files or "No other files generated yet.",
    )
    return _run_agentic(
        prompt,
        f"[{AGENT_ID}] codegen:{file_info.get('path', '')}",
        system_prompt=_build_web_system_prompt(prompts.CODEGEN_SYSTEM),
        timeout=180,
        max_tokens=8192,
    )


def _normalize_plan_path(path: str) -> str:
    normalized = (path or "").strip().replace("\\", "/")
    # Strip leading ./ or / prefixes only as complete units to avoid removing
    # legitimate leading dots (e.g. .github/, .gitignore)
    while normalized.startswith("./") or normalized.startswith("/"):
        normalized = normalized[2:] if normalized.startswith("./") else normalized[1:]
    if not normalized:
        return normalized
    dir_name, base_name = os.path.split(normalized)
    dotfile_aliases = {
        "gitignore": ".gitignore",
        "nvmrc": ".nvmrc",
        "dockerignore": ".dockerignore",
    }
    replacement = dotfile_aliases.get(base_name.lower())
    if replacement:
        normalized = os.path.join(dir_name, replacement) if dir_name else replacement
    return normalized.replace("\\", "/")


def _is_spa_router_file(path: str) -> bool:
    path_lower = path.lower()
    return bool(
        re.match(r"^src/(app|main|routes|router)\.[^/]+$", path_lower)
        or path_lower.startswith("src/routes/")
        or path_lower.startswith("src/router/")
    )


def _is_top_level_next_route_file(path: str) -> bool:
    path_lower = path.lower()
    return path_lower.startswith("pages/") or path_lower.startswith("app/")


def _is_operational_plan_artifact(file_info: dict) -> bool:
    path_lower = _normalize_plan_path(str(file_info.get("path", ""))).lower()
    purpose_lower = str(file_info.get("purpose", "")).lower()
    logic_lower = str(file_info.get("key_logic", "")).lower()
    text = " ".join(part for part in (path_lower, purpose_lower, logic_lower) if part)
    if not path_lower:
        return True
    if "pull request body" in text or "pr description" in text or "jira evidence" in text:
        return True
    if path_lower.startswith("artifacts/"):
        return True
    # Reject work/ and .work/ evidence directories (screenshots, test logs, CI evidence, Jira API responses)
    if path_lower.startswith("work/") or "/work/" in path_lower:
        return True
    if path_lower.startswith(".work/") or "/.work/" in path_lower:
        return True
    # Reject scripts/ helper folders (branch/PR scripts, Jira update scripts, etc.)
    # Note: do NOT include "script" — too broad; it would match any JS file described as a script
    if path_lower.startswith("scripts/") and any(
        kw in text for kw in ("jira", "branch", "pr", "update", "instructions", "helper")
    ):
        return True
    base_name = os.path.basename(path_lower)
    if re.match(r"^step-\d+.*\.md$", base_name):
        return True
    if re.match(r"^pr[_-]?template", base_name):
        return True
    # Reject common evidence/scratch file names regardless of directory
    if re.match(r"^(jira[_-]update|branch[_-]and[_-]pr|server[_-]curl|pytest[_-]output)\.(sh|txt|json|md)$", base_name):
        return True
    return False


def _sanitize_plan_files(files: list[dict], analysis: dict, review_issues: list[str]) -> tuple[list[dict], list[dict]]:
    """Remove conflicting or non-repo plan entries before code generation."""
    normalized_files: list[dict] = []
    removed: list[dict] = []
    seen_paths: set[str] = set()

    for file_info in files or []:
        normalized_path = _normalize_plan_path(str(file_info.get("path", "")))
        if not normalized_path:
            removed.append({"path": "", "reason": "empty plan path"})
            continue
        path_key = normalized_path.lower()
        if path_key in seen_paths:
            removed.append({"path": normalized_path, "reason": "duplicate plan path"})
            continue
        candidate = dict(file_info)
        candidate["path"] = normalized_path
        normalized_files.append(candidate)
        seen_paths.add(path_key)

    if not normalized_files:
        return normalized_files, removed

    frontend = str(analysis.get("frontend_framework", "")).strip().lower()
    issue_text = "\n".join(str(issue) for issue in (review_issues or [])).lower()
    prefer_nextjs = frontend == "nextjs" or ("next.js" in issue_text and "remove spa" in issue_text)
    prefer_react = frontend == "react" or ("react router" in issue_text and "remove next" in issue_text)
    has_top_level_next_routes = any(
        _is_top_level_next_route_file(file_info["path"])
        for file_info in normalized_files
    )

    kept: list[dict] = []
    kept_paths: set[str] = set()
    for file_info in normalized_files:
        path = file_info["path"]
        path_lower = path.lower()
        base_name = os.path.basename(path_lower)
        reason = ""

        if base_name.startswith(".env") and ".example" not in base_name:
            reason = "drop environment-specific file; keep only example env templates"
        elif _is_operational_plan_artifact(file_info):
            reason = "workflow evidence belongs in workspace artifacts, not repo file plan"
        elif prefer_nextjs and _is_spa_router_file(path):
            reason = "drop SPA router shell for Next.js implementation"
        elif prefer_nextjs and has_top_level_next_routes and (
            path_lower.startswith("src/pages/") or path_lower.startswith("src/app/")
        ):
            reason = "drop duplicate src route tree when top-level Next.js routes are present"
        elif prefer_react and (
            path_lower.startswith("pages/")
            or path_lower.startswith("app/")
            or re.search(r"\.next\.test\.[^.]+$", path_lower)
        ):
            reason = "drop Next.js-specific files for React SPA implementation"

        if reason:
            removed.append({"path": path, "reason": reason})
            continue

        kept.append(file_info)
        kept_paths.add(path_lower)

    if prefer_nextjs and has_top_level_next_routes and kept:
        final_kept: list[dict] = []
        for file_info in kept:
            path_lower = file_info["path"].lower()
            if path_lower.startswith("src/pages/__tests__/") and not any(
                route in kept_paths for route in ("pages/index.tsx", "pages/index.jsx", "app/page.tsx", "app/page.jsx")
            ):
                removed.append({
                    "path": file_info["path"],
                    "reason": "drop orphaned SPA page test after Next.js route sanitization",
                })
                continue
            final_kept.append(file_info)
        if final_kept:
            kept = final_kept

    return kept or normalized_files, removed


def _generate_pr_description(
    task_instruction: str,
    acceptance_criteria: list,
    files_changed: list,
    implementation_summary: str,
    design_context_meta: dict | None = None,
    test_output: str = "",
    repo_url: str = "",
    branch_name: str = "",
) -> tuple[str, str]:
    """Return (pr_title, pr_body)."""
    criteria_text = "\n".join(f"- {c}" for c in (acceptance_criteria or [])) or "Not specified."
    files_text = "\n".join(f"- {f}" for f in files_changed) or "No files listed."

    # Build design reference block
    design_parts = []
    if design_context_meta:
        if design_context_meta.get("url"):
            design_parts.append(f"Design URL: {design_context_meta['url']}")
        if design_context_meta.get("page_name"):
            design_parts.append(f"Screen: {design_context_meta['page_name']}")
        if design_context_meta.get("thumbnailUrl"):
            design_parts.append(f"thumbnail_url: {design_context_meta['thumbnailUrl']}")
    design_reference = "\n".join(design_parts) if design_parts else "No design reference provided."

    # Build viewport-based screenshots block (GitHub raw URLs when available)
    # Files are named docs/evidence/screenshot-{W}x{H}.png — no platform labels.
    _raw_base = ""
    if repo_url and branch_name:
        _raw_base = repo_url.rstrip("/").replace(
            "https://github.com/", "https://raw.githubusercontent.com/"
        )
    screenshots_lines: list[str] = []
    for _w, _h in _UI_SCREENSHOT_VIEWPORTS:
        _rel = f"docs/evidence/screenshot-{_w}x{_h}.png"
        if _rel in files_changed:
            if _raw_base:
                _url = f"{_raw_base}/{branch_name}/{_rel}"
                screenshots_lines.append(f"### {_w}×{_h}\n![Screenshot {_w}x{_h}]({_url})")
            else:
                screenshots_lines.append(f"### {_w}×{_h}\nSee `{_rel}` in committed files.")
    screenshots_block = (
        "\n\n".join(screenshots_lines)
        if screenshots_lines
        else "No UI screenshots (backend-only or screenshot capture unavailable)."
    )

    # Build test evidence block
    if test_output:
        test_evidence = test_output[:800]
    else:
        test_evidence = "No test output captured."

    prompt = prompts.PR_DESCRIPTION_TEMPLATE.format(
        task_instruction=task_instruction,
        acceptance_criteria=criteria_text,
        files_changed=files_text,
        implementation_summary=implementation_summary,
        design_reference=design_reference,
        test_evidence=test_evidence,
        screenshots_block=screenshots_block,
    )
    response = _run_agentic(
        prompt,
        f"[{AGENT_ID}] pr-description",
        system_prompt=_build_web_system_prompt(prompts.PR_DESCRIPTION_SYSTEM),
    )
    lines = response.strip().splitlines()
    title = lines[0].strip() if lines else "Web Agent: implement task"
    body = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""

    # Post-process: force the screenshots block verbatim into the body.
    # LLMs reliably strip the leading '!' from ![alt](url) syntax, turning
    # inline images into plain links.  We bypass that by replacing the entire
    # ## Screenshots section after the LLM call.
    if screenshots_block and "No UI screenshots" not in screenshots_block:
        import re as _re
        _section = f"## Screenshots\n\n{screenshots_block}"
        body, _replaced = _re.subn(
            r"## Screenshots\b.*?(?=\n## |\Z)",
            _section,
            body,
            count=1,
            flags=_re.DOTALL,
        )
        if not _replaced:
            body = body + f"\n\n{_section}"

    return title, body


def _generate_summary(
    task_instruction: str,
    acceptance_criteria: list,
    files_list: list,
    pr_url: str,
) -> str:
    criteria_text = "\n".join(f"- {c}" for c in (acceptance_criteria or [])) or "Not specified."
    files_text = "\n".join(f"- {f}" for f in files_list) or "No files generated."
    prompt = prompts.SUMMARY_TEMPLATE.format(
        task_instruction=task_instruction,
        files_list=files_text,
        pr_url=pr_url or "No PR created.",
        acceptance_criteria=criteria_text,
    )
    try:
        return _run_agentic(
            prompt,
            f"[{AGENT_ID}] summary",
            system_prompt=_build_web_system_prompt(prompts.SUMMARY_SYSTEM),
        )
    except Exception as err:
        return f"Web Agent completed. Summary unavailable: {err}"


# ---------------------------------------------------------------------------
# SCM / Jira helpers
# ---------------------------------------------------------------------------

def _fetch_jira_context(
    task_id: str,
    ticket_key: str,
    workspace: str,
    compass_task_id: str,
    permissions: dict | None = None,
) -> str:
    """Fetch Jira ticket content via Jira Agent."""
    workflow_task_id = compass_task_id or task_id
    try:
        result = _jira_request_json(
            "jira.ticket.fetch",
            "GET",
            f"/jira/tickets/{ticket_key}",
            permissions=permissions,
            workspace=workspace,
            task_id=task_id,
            compass_task_id=compass_task_id,
        )
        issue = result.get("issue") or {}
        content = json.dumps(issue, ensure_ascii=False, indent=2) if issue else ""
        _record_jira_action(
            workspace,
            workflow_task_id,
            ticket_key,
            "fetch",
            "completed",
            agent_task_id=task_id,
            contentLength=len(content),
        )
        return content
    except Exception as err:
        print(f"[{AGENT_ID}] Could not fetch Jira ticket {ticket_key}: {err}")
        _record_jira_action(
            workspace,
            workflow_task_id,
            ticket_key,
            "fetch",
            "failed",
            agent_task_id=task_id,
            error=str(err),
        )
        return ""


def _jira_transition(
    ticket_key: str,
    target_status: str,
    task_id: str,
    workspace: str,
    compass_task_id: str,
    permissions: dict | None = None,
):
    """Transition a Jira ticket to a new status (best-effort, non-blocking)."""
    workflow_task_id = compass_task_id or task_id
    try:
        result = _jira_request_json(
            "jira.ticket.transition",
            "POST",
            f"/jira/transitions/{ticket_key}",
            payload={"transition": target_status},
            permissions=permissions,
            workspace=workspace,
            task_id=task_id,
            compass_task_id=compass_task_id,
        )
        print(f"[{AGENT_ID}] Jira {ticket_key} transitioned to '{target_status}'")
        _record_jira_action(
            workspace,
            workflow_task_id,
            ticket_key,
            "transition",
            "completed",
            agent_task_id=task_id,
            targetStatus=target_status,
            result=result.get("result"),
        )
    except Exception as err:
        print(f"[{AGENT_ID}] Jira transition failed (non-critical): {err}")
        _record_jira_action(
            workspace,
            workflow_task_id,
            ticket_key,
            "transition",
            "failed",
            agent_task_id=task_id,
            targetStatus=target_status,
            error=str(err),
        )


def _jira_assign_self(
    ticket_key: str,
    task_id: str,
    workspace: str,
    compass_task_id: str,
    permissions: dict | None = None,
):
    """Assign the Jira ticket to the bot (service account) that owns the credentials (best-effort)."""
    workflow_task_id = compass_task_id or task_id
    try:
        account_id = _get_jira_account_id(
            workspace,
            task_id,
            permissions=permissions,
            compass_task_id=compass_task_id,
        )
        result = _jira_request_json(
            "jira.ticket.assignee",
            "PUT",
            f"/jira/assignee/{ticket_key}",
            payload={"accountId": account_id},
            permissions=permissions,
            workspace=workspace,
            task_id=task_id,
            compass_task_id=compass_task_id,
        )
        print(f"[{AGENT_ID}] Jira {ticket_key} assigned to service account")
        _record_jira_action(
            workspace,
            workflow_task_id,
            ticket_key,
            "assign",
            "completed",
            agent_task_id=task_id,
            accountId=account_id,
            result=result.get("result"),
        )
    except Exception as err:
        print(f"[{AGENT_ID}] Jira assign failed (non-critical): {err}")
        _record_jira_action(
            workspace,
            workflow_task_id,
            ticket_key,
            "assign",
            "failed",
            agent_task_id=task_id,
            error=str(err),
        )


def _jira_add_comment(
    ticket_key: str,
    comment: str,
    task_id: str,
    workspace: str,
    compass_task_id: str,
    permissions: dict | None = None,
    *,
    adf_body: dict | None = None,
    comment_preview: str = "",
):
    """Add a comment to a Jira ticket (best-effort, non-blocking)."""
    workflow_task_id = compass_task_id or task_id
    try:
        payload = {"adf": adf_body} if adf_body else {"text": comment}
        result = _jira_request_json(
            "jira.comment.add",
            "POST",
            f"/jira/comments/{ticket_key}",
            payload=payload,
            permissions=permissions,
            workspace=workspace,
            task_id=task_id,
            compass_task_id=compass_task_id,
        )
        preview = comment_preview or comment or _adf_plain_text(adf_body)
        print(f"[{AGENT_ID}] Jira {ticket_key} comment added")
        _record_jira_action(
            workspace,
            workflow_task_id,
            ticket_key,
            "comment",
            "completed",
            agent_task_id=task_id,
            commentPreview=preview[:240],
            commentId=result.get("commentId"),
            result=result.get("result"),
        )
    except Exception as err:
        preview = comment_preview or comment or _adf_plain_text(adf_body)
        print(f"[{AGENT_ID}] Jira comment failed (non-critical): {err}")
        _record_jira_action(
            workspace,
            workflow_task_id,
            ticket_key,
            "comment",
            "failed",
            agent_task_id=task_id,
            commentPreview=preview[:240],
            error=str(err),
        )


def _clone_repo(
    task_id: str,
    repo_url: str,
    workspace: str,
    compass_task_id: str,
    permissions: dict | None = None,
) -> str:
    """Clone repository via SCM Agent and return the clone path."""
    result = _call_sync_agent(
        "scm.git.clone",
        f"Clone repository {repo_url} to {workspace}",
        task_id,
        workspace,
        compass_task_id,
        permissions=permissions,
    )
    # Primary: extract clone path from artifacts (JSON)
    for art in result.get("artifacts", []):
        text = artifact_text(art)
        if text:
            try:
                data = json.loads(text)
                path = data.get("clone_path") or data.get("clonePath") or ""
                if path:
                    return path
            except Exception:
                if text.strip().startswith("/"):
                    return text.strip()
    # Fallback: check extra dict returned by SCM task payload
    extra = result.get("extra", {})
    if extra.get("clonePath"):
        return extra["clonePath"]

    state = result.get("status", {}).get("state", "")
    status_text = extract_text(result.get("status", {}).get("message", {})).strip()
    if state in {"TASK_STATE_FAILED", "FAILED"}:
        raise RuntimeError(status_text or f"SCM clone failed for {repo_url}")
    if state in {"TASK_STATE_COMPLETED", "COMPLETED"}:
        raise RuntimeError(f"SCM clone completed without a clone path for {repo_url}")
    raise RuntimeError(f"SCM clone did not reach terminal success state for {repo_url} (state={state or 'unknown'})")


def _create_branch(
    task_id: str,
    repo_url: str,
    branch_name: str,
    base_branch: str,
    workspace: str,
    compass_task_id: str,
    permissions: dict | None = None,
) -> bool:
    """Create a feature branch via SCM Agent."""
    try:
        result = _call_sync_agent(
            "scm.branch.create",
            f"Create branch {branch_name} from {base_branch} in {repo_url}",
            task_id,
            workspace,
            compass_task_id,
            permissions=permissions,
        )
        state = result.get("status", {}).get("state", "")
        return state in ("TASK_STATE_COMPLETED", "COMPLETED")
    except Exception as err:
        print(f"[{AGENT_ID}] Could not create branch {branch_name}: {err}")
        return False


def _push_files(
    task_id: str,
    repo_url: str,
    branch_name: str,
    files: list[dict],
    commit_message: str,
    workspace: str,
    compass_task_id: str,
    permissions: dict | None = None,
    base_branch: str = "main",
) -> bool:
    """Push generated files to feature branch via SCM Agent.
    Passes structured pushPayload in metadata so the SCM agent does not need
    to parse owner/repo/branch from free-form text.
    """
    # Extract owner/repo from repo_url for structured payload
    owner, repo = "", ""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s?#]+)", repo_url or "")
    if m:
        owner = m.group(1)
        repo = m.group(2).rstrip(".git")

    try:
        scm_service_url = _resolve_agent_service_url("scm.git.push")
        message = {
            "messageId": f"web-{task_id}-push-{int(time.time())}",
            "role": "ROLE_USER",
            "parts": [{"text": f"Push files to branch {branch_name} in {repo_url}"}],
            "metadata": {
                "requestedCapability": "scm.git.push",
                "orchestratorTaskId": compass_task_id,
                "sharedWorkspacePath": workspace,
                "pushPayload": {
                    "owner": owner,
                    "repo": repo,
                    "branch": branch_name,
                    "baseBranch": base_branch,
                    "files": files,
                    "commitMessage": commit_message,
                },
            },
        }
        if isinstance(permissions, dict) and permissions:
            message["metadata"]["permissions"] = permissions
        downstream = _a2a_send(scm_service_url, message)
        task_id_ds = downstream.get("id", "")
        state = downstream.get("status", {}).get("state", "")
        terminal = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED"}
        if state not in terminal and task_id_ds:
            result = _poll_task(scm_service_url, task_id_ds, timeout=SYNC_AGENT_TIMEOUT)
            if result:
                downstream = result
        state = downstream.get("status", {}).get("state", "")
        if state not in ("TASK_STATE_COMPLETED", "COMPLETED"):
            msg = downstream.get("status", {}).get("message", {})
            txt = (msg.get("parts") or [{}])[0].get("text", "")
            print(f"[{AGENT_ID}] Push failed: {txt[:200]}")
            return False
        return True
    except Exception as err:
        print(f"[{AGENT_ID}] Could not push files to {branch_name}: {err}")
        return False


def _sanitize_base_branch(branch: str) -> str:
    """Return a safe base branch name. Fall back to 'main' if the value looks
    like a Jira ticket key or other non-branch string."""
    if not branch:
        return "main"
    # Reject patterns like PROJ-123, PROJ-1/landing-page, jira-key/foo
    if re.match(r"^[A-Z][A-Z0-9]+-\d+", branch):
        return "main"
    # Also reject if it contains characters not valid in branch names
    if re.search(r"[\s~^:?*\[\\]", branch):
        return "main"
    return branch


def _create_pr(
    task_id: str,
    repo_url: str,
    branch_name: str,
    base_branch: str,
    pr_title: str,
    pr_body: str,
    workspace: str,
    compass_task_id: str,
    permissions: dict | None = None,
) -> str:
    """Create a pull request via SCM Agent. Returns PR URL.
    Passes structured prPayload in metadata to avoid unreliable text parsing.
    """
    # Sanitize base branch — LLM may return Jira-key-style strings
    safe_base = _sanitize_base_branch(base_branch)

    # Extract owner/repo from URL for structured payload
    owner, repo = "", ""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s?#]+)", repo_url or "")
    if m:
        owner = m.group(1)
        repo = m.group(2).rstrip(".git")

    try:
        scm_service_url = _resolve_agent_service_url("scm.pr.create")
        message = {
            "messageId": f"web-{task_id}-pr-{int(time.time())}",
            "role": "ROLE_USER",
            "parts": [{
                "text": (
                    f"Create pull request from {branch_name} to {safe_base} in {repo_url}.\n"
                    f"Title: {pr_title}"
                )
            }],
            "metadata": {
                "requestedCapability": "scm.pr.create",
                "orchestratorTaskId": compass_task_id,
                "sharedWorkspacePath": workspace,
                "prPayload": {
                    "owner": owner,
                    "repo": repo,
                    "fromBranch": branch_name,
                    "toBranch": safe_base,
                    "title": pr_title,
                    "description": pr_body,
                },
            },
        }
        if isinstance(permissions, dict) and permissions:
            message["metadata"]["permissions"] = permissions
        downstream = _a2a_send(scm_service_url, message)
        task_id_ds = downstream.get("id", "")
        state = downstream.get("status", {}).get("state", "")
        terminal = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED"}
        if state not in terminal and task_id_ds:
            result = _poll_task(scm_service_url, task_id_ds, timeout=SYNC_AGENT_TIMEOUT)
            if result:
                downstream = result
        for art in (downstream.get("artifacts") or []):
            text = artifact_text(art)
            if text:
                try:
                    data = json.loads(text)
                    url = (
                        data.get("htmlUrl") or data.get("html_url")
                        or data.get("pr_url") or data.get("prUrl")
                        or data.get("url") or ""
                    )
                    if url:
                        return url
                except Exception:
                    url_match = re.search(r"https?://\S+", text)
                    if url_match:
                        return url_match.group()
        # Also check error message from status
        msg = downstream.get("status", {}).get("message", {})
        err_txt = (msg.get("parts") or [{}])[0].get("text", "")
        if err_txt:
            print(f"[{AGENT_ID}] PR create status: {err_txt[:200]}")
        return ""
    except Exception as err:
        print(f"[{AGENT_ID}] Could not create PR: {err}")
        return ""



def _read_repo_snapshot(clone_path: str, max_files: int = 30, max_chars: int = 8000) -> str:
    """Read key files from a cloned repo to provide context for LLM."""
    if not clone_path or not os.path.isdir(clone_path):
        return ""

    snapshot_parts: list[str] = []
    chars_used = 0
    files_read = 0

    # Priority files to include first
    priority_patterns = [
        "package.json", "pyproject.toml", "setup.py", "requirements.txt",
        "README.md", "tsconfig.json", ".env.example",
    ]

    def _read_file_safe(filepath: str, limit: int = 1500) -> str:
        try:
            with open(filepath, encoding="utf-8", errors="replace") as fh:
                content = fh.read(limit)
            if len(content) == limit:
                content += "\n...[truncated]"
            return content
        except Exception:
            return ""

    # Read priority files first
    for pattern in priority_patterns:
        candidate = os.path.join(clone_path, pattern)
        if os.path.isfile(candidate) and chars_used < max_chars and files_read < max_files:
            content = _read_file_safe(candidate)
            if content:
                rel = os.path.relpath(candidate, clone_path)
                snapshot_parts.append(f"=== {rel} ===\n{content}")
                chars_used += len(content)
                files_read += 1

    # Walk the tree for source files
    skip_dirs = {
        ".git", "node_modules", "__pycache__", ".next", "dist",
        "build", "out", "venv", ".venv", ".cache",
    }
    source_exts = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md",
        ".html", ".css", ".scss",
    }

    for root, dirs, files in os.walk(clone_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in sorted(files):
            if files_read >= max_files or chars_used >= max_chars:
                break
            _, ext = os.path.splitext(fname)
            if ext not in source_exts:
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, clone_path)
            # Skip if already included as priority
            if fname in priority_patterns:
                continue
            content = _read_file_safe(fpath)
            if content:
                snapshot_parts.append(f"=== {rel} ===\n{content}")
                chars_used += len(content)
                files_read += 1

    return "\n\n".join(snapshot_parts)


def _install_plan_dependencies(deps: list[str], language: str, log_fn):
    """Install dependencies declared in the plan at runtime (best-effort)."""
    if not deps:
        return
    python_pkgs = []
    npm_pkgs = []
    for dep in deps:
        dep = dep.strip()
        if not dep:
            continue
        # Simple heuristic: if it looks like a PyPI package (no @, no /) it's Python;
        # if it starts with @ or is a scoped package it's npm.
        if dep.startswith("@") or "/" in dep or language in ("javascript", "typescript"):
            npm_pkgs.append(dep)
        else:
            python_pkgs.append(dep)

    if python_pkgs:
        log_fn(f"Installing Python packages: {', '.join(python_pkgs)}")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet"] + python_pkgs,
                timeout=120,
                check=False,
            )
        except Exception as err:
            log_fn(f"Warning: pip install failed: {err}")

    if npm_pkgs:
        log_fn(f"Installing npm packages: {', '.join(npm_pkgs)}")
        try:
            subprocess.run(
                ["npm", "install", "--save"] + npm_pkgs,
                timeout=120,
                check=False,
            )
        except Exception as err:
            log_fn(f"Warning: npm install failed: {err}")


def _install_written_node_dependencies(build_dir: str, log_fn) -> None:
    """Install npm dependencies after generated package manifests have been written."""
    root_manifest = _load_package_json(os.path.join(build_dir, "package.json"))
    if not root_manifest:
        return

    package_dirs: list[str] = [build_dir]
    has_workspaces = isinstance(root_manifest.get("workspaces"), (list, dict))
    if not has_workspaces:
        for rel_dir in ("client", "server"):
            if _load_package_json(os.path.join(build_dir, rel_dir, "package.json")):
                package_dirs.append(os.path.join(build_dir, rel_dir))

    seen_dirs: set[str] = set()
    for package_dir in package_dirs:
        if package_dir in seen_dirs:
            continue
        seen_dirs.add(package_dir)
        rel_dir = os.path.relpath(package_dir, build_dir)
        label = "." if rel_dir == "." else rel_dir
        log_fn(f"Installing npm dependencies from generated manifests ({label})")
        try:
            subprocess.run(
                ["npm", "install", "--no-fund", "--no-audit"],
                cwd=package_dir,
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
                env={**os.environ, "CI": "true"},
            )
        except Exception as err:
            log_fn(f"Warning: npm install from generated manifests failed in {label}: {err}")


def _write_files_to_directory(base_dir: str, files: list[dict]) -> list[str]:
    """Write generated code files into the specified directory."""
    if not base_dir:
        return []
    os.makedirs(base_dir, exist_ok=True)
    written: list[str] = []
    for file_info in files:
        rel_path = file_info.get("path", "output.txt").lstrip("/")
        full_path = os.path.join(base_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        content = file_info.get("content", "")
        try:
            with open(full_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(full_path)
        except Exception as err:
            print(f"[{AGENT_ID}] Could not write {full_path}: {err}")
    return written


def _project_uses_python(build_dir: str, language: str) -> bool:
    return language in ("python", "mixed") or any(
        os.path.isfile(os.path.join(build_dir, candidate))
        for candidate in ("requirements.txt", "pyproject.toml", "setup.py")
    )


def _ensure_local_python_env(build_dir: str, language: str, log_fn) -> str:
    if not _project_uses_python(build_dir, language):
        return sys.executable

    # Create the venv OUTSIDE the repo directory so that:
    # 1. Its shebangs use a short, absolute path (avoids OS shebang-length limits)
    # 2. It is not accidentally committed to the repo
    # 3. It remains usable if only the repo directory is shared between Docker and host
    import hashlib
    import tempfile
    build_hash = hashlib.md5(build_dir.encode()).hexdigest()[:12]
    venv_dir = os.path.join(tempfile.gettempdir(), f"constellation-venv-{build_hash}")
    venv_python = os.path.join(venv_dir, "bin", "python")
    requirements_path = os.path.join(build_dir, "requirements.txt")
    install_stamp = os.path.join(venv_dir, ".requirements-installed")

    try:
        if not os.path.isfile(venv_python):
            log_fn(f"Creating Python virtual environment at {venv_dir}")
            subprocess.run(
                [sys.executable, "-m", "venv", venv_dir],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )

        if os.path.isfile(requirements_path):
            requirements_mtime = os.path.getmtime(requirements_path)
            stamp_mtime = os.path.getmtime(install_stamp) if os.path.isfile(install_stamp) else 0
            if requirements_mtime > stamp_mtime:
                log_fn("Installing local Python dependencies from requirements.txt")
                subprocess.run(
                    [venv_python, "-m", "pip", "install", "--quiet", "-r", requirements_path],
                    cwd=build_dir,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=True,
                )
                with open(install_stamp, "w", encoding="utf-8") as handle:
                    handle.write(local_iso_timestamp())
        return venv_python
    except Exception as err:
        log_fn(f"Warning: could not prepare local Python environment: {err}")
        return sys.executable


def _run_local_git(repo_dir: str, args: list[str], *, check: bool = True) -> tuple[bool, str]:
    env = build_isolated_git_env(scope=f"{AGENT_ID}-local-git")
    result = subprocess.run(
        ["git", "-c", "credential.helper=", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    output = (result.stdout or result.stderr or "").strip()
    if check and result.returncode != 0:
        raise RuntimeError(output or f"git {' '.join(args)} failed")
    return result.returncode == 0, output


def _local_branch_exists(repo_dir: str, branch_name: str) -> bool:
    ok, _ = _run_local_git(
        repo_dir,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        check=False,
    )
    return ok


def _checkout_local_branch(repo_dir: str, branch_name: str, base_branch: str, log_fn) -> None:
    if _local_branch_exists(repo_dir, branch_name):
        _run_local_git(repo_dir, ["checkout", branch_name])
        log_fn(f"Checked out existing local branch: {branch_name}")
        return

    ok, output = _run_local_git(repo_dir, ["checkout", "-B", branch_name, base_branch], check=False)
    if not ok:
        ok, output = _run_local_git(repo_dir, ["checkout", "-b", branch_name], check=False)
    if not ok:
        raise RuntimeError(output or f"Could not create local branch {branch_name}")
    log_fn(f"Created local branch: {branch_name}")


def _delete_local_branch(repo_dir: str, branch_name: str, base_branch: str) -> None:
    _run_local_git(repo_dir, ["checkout", base_branch], check=False)
    _run_local_git(repo_dir, ["branch", "-D", branch_name], check=False)


def _sanitize_branch_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "")
    sanitized = sanitized.strip("-._")
    return sanitized or "task"


def _is_docs_or_tests_only(paths: list[str]) -> bool:
    if not paths:
        return False
    for raw_path in paths:
        path = (raw_path or "").strip().lstrip("/").lower()
        if not path:
            continue
        if path.startswith("docs/") or path.startswith("tests/"):
            continue
        if "/tests/" in path or path.endswith(("_test.py", ".spec.ts", ".spec.js", ".test.ts", ".test.js", ".test.tsx", ".test.jsx")):
            continue
        if path.endswith((".md", ".rst", ".txt")) or os.path.basename(path) in {"readme.md", "running.md"}:
            continue
        return False
    return True


def _classify_branch_kind(
    task_instruction: str,
    analysis: dict,
    planned_paths: list[str],
    ticket_key: str,
) -> str:
    if not ticket_key:
        if _is_docs_or_tests_only(planned_paths):
            return "chore"
        raise RuntimeError("Feature implementation and issue fixes require a Jira ticket.")

    summary_text = " ".join(
        part for part in [task_instruction, analysis.get("task_summary", "")] if part
    ).lower()
    hotfix_markers = ("bug", "fix", "hotfix", "regression", "error", "defect")
    feature_markers = ("feature", "implement", "build", "create", "add", "develop")
    if any(marker in summary_text for marker in hotfix_markers) and not any(
        marker in summary_text for marker in feature_markers
    ):
        return "hotfix"
    return "feature"


def _resolve_ticket_key(task_instruction: str, metadata: dict | None = None) -> str:
    jira_context = (metadata or {}).get("jiraContext")
    if isinstance(jira_context, dict):
        ticket_key_from_context = str(jira_context.get("ticketKey") or "").strip()
        if ticket_key_from_context:
            return ticket_key_from_context

    ticket_key_from_meta = str((metadata or {}).get("jiraTicketKey") or "").strip()
    if ticket_key_from_meta:
        return ticket_key_from_meta

    ticket_match = re.search(r"\b([A-Z][A-Z0-9]+-\d{2,})\b", task_instruction or "")
    return ticket_match.group(1) if ticket_match else ""


def _resolve_jira_context_from_metadata(
    task_instruction: str,
    metadata: dict | None = None,
) -> tuple[str, str]:
    jira_context = (metadata or {}).get("jiraContext")
    if not isinstance(jira_context, dict):
        jira_context = {}
    ticket_key = _resolve_ticket_key(task_instruction, metadata)
    content = str(jira_context.get("content") or "").strip()
    return ticket_key, content


def _list_remote_branches(
    task_id: str,
    repo_url: str,
    workspace: str,
    compass_task_id: str,
    permissions: dict | None = None,
) -> set[str]:
    try:
        result = _call_sync_agent(
            "scm.branch.list",
            f"List branches in {repo_url}",
            task_id,
            workspace,
            compass_task_id,
            permissions=permissions,
        )
    except Exception:
        return set()

    for art in result.get("artifacts", []):
        text = artifact_text(art)
        if not text:
            continue
        try:
            branches = json.loads(text)
        except Exception:
            continue
        if isinstance(branches, list):
            return {
                str(branch.get("name", "")).strip()
                for branch in branches
                if isinstance(branch, dict) and str(branch.get("name", "")).strip()
            }
    return set()


def _select_branch_name(
    task_instruction: str,
    analysis: dict,
    planned_paths: list[str],
    ticket_key: str,
    task_id: str,
    repo_url: str,
    clone_path: str,
    workspace: str,
    compass_task_id: str,
    permissions: dict | None = None,
) -> tuple[str, str]:
    branch_kind = _classify_branch_kind(task_instruction, analysis, planned_paths, ticket_key)
    workflow_task_id = _sanitize_branch_component(compass_task_id or task_id)
    remote_branches = _list_remote_branches(
        task_id,
        repo_url,
        workspace,
        compass_task_id,
        permissions=permissions,
    )

    if branch_kind == "chore":
        branch_base = f"chore/{workflow_task_id}"
    else:
        branch_base = f"{branch_kind}/{_sanitize_branch_component(ticket_key)}_{workflow_task_id}"

    for sequence in range(1, 100):
        candidate = f"{branch_base}_{sequence}"
        if candidate in remote_branches:
            continue
        if clone_path and _local_branch_exists(clone_path, candidate):
            continue
        return candidate, branch_kind

    raise RuntimeError(f"Could not allocate a unique branch name for {branch_base}")


def _commit_local_changes(
    repo_dir: str,
    branch_name: str,
    files: list[dict],
    commit_message: str,
    log_fn,
) -> str:
    _run_local_git(repo_dir, ["config", "user.email", "web-agent@local"], check=False)
    _run_local_git(repo_dir, ["config", "user.name", "Web Agent"], check=False)

    staged_any = False
    for file_info in files:
        rel_path = file_info.get("path", "").lstrip("/")
        if not rel_path:
            continue
        _run_local_git(repo_dir, ["add", "--", rel_path], check=False)
        staged_any = True

    if not staged_any:
        return ""

    ok, status_output = _run_local_git(repo_dir, ["status", "--porcelain"], check=False)
    if not ok or not status_output.strip():
        ok, head_output = _run_local_git(repo_dir, ["rev-parse", "HEAD"], check=False)
        return head_output.strip() if ok else ""

    _run_local_git(repo_dir, ["commit", "-m", commit_message])
    _, head_output = _run_local_git(repo_dir, ["rev-parse", "HEAD"])
    commit_sha = head_output.strip()
    log_fn(f"Committed local changes on {branch_name}: {commit_sha[:12]}")
    return commit_sha


# ---------------------------------------------------------------------------
# Build / test execution with LLM-guided error recovery
# ---------------------------------------------------------------------------

MAX_BUILD_RETRIES = 3


def _detect_build_command(
    build_dir: str,
    language: str,
    python_executable: str | None = None,
) -> list[str] | None:
    """Return the command to run tests, or None if no test harness detected."""
    python_cmd = python_executable or sys.executable
    # Python: pytest or unittest
    if language in ("python", "mixed") or any(
        os.path.isfile(os.path.join(build_dir, f))
        for f in ("requirements.txt", "pyproject.toml", "setup.py")
    ):
        if any(
            fname.startswith("test_") or fname.endswith("_test.py")
            for _, _, files in os.walk(build_dir)
            for fname in files
        ):
            return [python_cmd, "-m", "pytest", "--tb=short", "-q", build_dir]
        # Fall back to running the main module if present
        for candidate in ("main.py", "app.py", "run.py"):
            if os.path.isfile(os.path.join(build_dir, candidate)):
                return [python_cmd, "-c",
                        f"import ast, sys; ast.parse(open('{os.path.join(build_dir, candidate)}').read());"
                        f"print('Syntax OK: {candidate}')"]
    return None


def _load_package_json(package_json_path: str) -> dict:
    if not os.path.isfile(package_json_path):
        return {}
    try:
        with open(package_json_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _package_uses_jest(manifest: dict) -> bool:
    scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
    for section_name in ("dependencies", "devDependencies"):
        section = manifest.get(section_name)
        if isinstance(section, dict) and "jest" in section:
            return True
    script_text = "\n".join(str(value) for value in scripts.values()).lower()
    return "jest" in script_text


def _detect_node_build_steps(build_dir: str) -> list[dict]:
    """Return npm test/build steps for root or client/server workspaces."""
    steps: list[dict] = []

    def _append_steps(manifest: dict, cwd: str, label: str, *, include_test: bool, include_build: bool) -> None:
        scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
        if include_test and "test" in scripts:
            command = ["npm", "test"]
            if _package_uses_jest(manifest):
                command.extend(["--", "--runInBand", "--coverage"])
            steps.append({"cwd": cwd, "cmd": command, "label": f"{label}:test"})
        if include_build and "build" in scripts:
            steps.append({"cwd": cwd, "cmd": ["npm", "run", "build"], "label": f"{label}:build"})

    root_manifest = _load_package_json(os.path.join(build_dir, "package.json"))
    root_scripts = root_manifest.get("scripts") if isinstance(root_manifest.get("scripts"), dict) else {}
    root_has_test = "test" in root_scripts
    root_has_build = "build" in root_scripts
    if root_manifest:
        _append_steps(
            root_manifest,
            build_dir,
            "root",
            include_test=True,
            include_build=True,
        )

    for rel_dir in ("client", "server"):
        manifest = _load_package_json(os.path.join(build_dir, rel_dir, "package.json"))
        if not manifest:
            continue
        _append_steps(
            manifest,
            os.path.join(build_dir, rel_dir),
            rel_dir,
            include_test=not root_has_test,
            include_build=not root_has_build,
        )

    return steps


def _run_build(build_dir: str, language: str, python_executable: str | None = None) -> tuple[bool, str]:
    """Run the build/test command in build_dir. Returns (success, output)."""
    python_cmd = python_executable or sys.executable
    node_steps = _detect_node_build_steps(build_dir)
    if node_steps:
        import shutil

        if not shutil.which("npm"):
            return False, "npm not found for Node.js build/test workflow."

        outputs: list[str] = []
        env = {**os.environ, "CI": "true"}
        try:
            for step in node_steps:
                result = subprocess.run(
                    step["cmd"],
                    cwd=step["cwd"],
                    capture_output=True,
                    text=True,
                    timeout=240,
                    env=env,
                )
                step_output = (result.stdout + "\n" + result.stderr).strip()
                rel_cwd = os.path.relpath(step["cwd"], build_dir)
                display_cwd = "." if rel_cwd == "." else rel_cwd
                outputs.append(f"$ {' '.join(step['cmd'])} ({display_cwd})\n{step_output}".strip())
                if result.returncode != 0:
                    return False, "\n\n".join(outputs)
            return True, "\n\n".join(outputs)
        except subprocess.TimeoutExpired:
            return False, "Node.js build/test timed out after 240 seconds."
        except Exception as exc:
            return False, f"Could not run Node.js build/test workflow: {exc}"

    cmd = _detect_build_command(build_dir, language, python_executable=python_cmd)
    if cmd is None:
        # No test harness — validate Python syntax of every .py file
        errors = []
        for root, _, files in os.walk(build_dir):
            for fname in files:
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, encoding="utf-8") as fh:
                            import ast as _ast
                            _ast.parse(fh.read())
                    except SyntaxError as exc:
                        errors.append(f"{fpath}: {exc}")
        if errors:
            return False, "\n".join(errors)
        return True, "Syntax check passed (no test harness found)."

    try:
        result = subprocess.run(
            cmd,
            cwd=build_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        # Self-heal: if pytest is missing, install it and retry once
        if result.returncode != 0 and "No module named pytest" in output:
            print(f"[{AGENT_ID}] pytest missing — installing...")
            subprocess.run(
                [python_cmd, "-m", "pip", "install", "--quiet", "pytest"],
                timeout=60,
            )
            result = subprocess.run(
                cmd,
                cwd=build_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Build/test timed out after 120 seconds."
    except Exception as exc:
        return False, f"Could not run build command: {exc}"


def _read_source_files(build_dir: str, max_files: int = 20) -> list[dict]:
    """Read all source files from the build directory for LLM context."""
    files = []
    skip_dirs = {"__pycache__", ".pytest_cache", "node_modules", ".git", "venv", ".venv"}
    source_exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml", ".cfg", ".ini", ".txt"}
    for root, dirs, fnames in os.walk(build_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in sorted(fnames):
            if len(files) >= max_files:
                break
            _, ext = os.path.splitext(fname)
            if ext not in source_exts:
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, build_dir)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    content = fh.read(4000)
                files.append({"path": rel, "content": content})
            except Exception:
                pass
    return files


def _apply_llm_fixes(build_dir: str, fixes: list[dict]):
    """Apply LLM-suggested file fixes to the build directory."""
    for fix in fixes:
        rel_path = fix.get("path", "").lstrip("/")
        content = fix.get("content", "")
        if not rel_path or not content:
            continue
        full_path = os.path.join(build_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        try:
            with open(full_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            print(f"[{AGENT_ID}] Applied fix to {rel_path}")
        except Exception as err:
            print(f"[{AGENT_ID}] Could not apply fix to {rel_path}: {err}")


def _build_and_test_with_recovery(
    build_dir: str,
    task_instruction: str,
    language: str,
    log_fn,
) -> tuple[bool, str, list[dict]]:
    """
    Run build/tests in build_dir with up to MAX_BUILD_RETRIES LLM-guided fix cycles.
    Returns (passed, final_output).
    """
    attempts: list[dict] = []
    output = ""
    python_executable = _ensure_local_python_env(build_dir, language, log_fn)
    for attempt in range(1, MAX_BUILD_RETRIES + 1):
        log_fn(f"Build/test attempt {attempt}/{MAX_BUILD_RETRIES}")
        success, output = _run_build(build_dir, language, python_executable=python_executable)
        attempts.append(
            {
                "attempt": attempt,
                "success": success,
                "output": output[:4000],
            }
        )
        if success:
            log_fn(f"Build/test passed on attempt {attempt}")
            return True, output, attempts

        log_fn(f"Build/test failed (attempt {attempt}): {output[:200]}")
        if attempt == MAX_BUILD_RETRIES:
            break

        # Ask LLM to diagnose and fix
        source_files = _read_source_files(build_dir)
        fix_prompt = prompts.BUILD_FIX_TEMPLATE.format(
            failure_output=output[:3000],
            source_files_json=json.dumps(source_files, ensure_ascii=False, indent=2)[:6000],
            task_instruction=task_instruction[:1000],
        )
        fix_response = _run_agentic(
            fix_prompt,
            f"[{AGENT_ID}] build-fix-attempt-{attempt}",
            system_prompt=_build_web_system_prompt(prompts.BUILD_FIX_SYSTEM),
            timeout=180,
            max_tokens=8192,
        )
        fix_data = _parse_json_from_llm(fix_response)
        diagnosis = fix_data.get("diagnosis", "unknown")
        fixes = fix_data.get("fixes") or []
        log_fn(f"LLM diagnosis: {diagnosis} — {len(fixes)} fix(es) to apply")

        if not fixes:
            log_fn("LLM produced no fixes — stopping retry loop")
            break

        _apply_llm_fixes(build_dir, fixes)

    return False, output, attempts


# ---------------------------------------------------------------------------
# .gitignore generation
# ---------------------------------------------------------------------------

def _generate_gitignore_content(analysis: dict) -> str:
    """Generate a .gitignore appropriate for the project's tech stack."""
    backend = str(analysis.get("backend_framework", "")).lower()
    frontend = str(analysis.get("frontend_framework", "")).lower()
    language = str(analysis.get("language", "")).lower()

    lines = []
    if language == "python" or backend in ("flask", "django", "fastapi"):
        lines += [
            "# Python",
            "__pycache__/",
            "*.py[cod]",
            "*$py.class",
            "*.so",
            "venv/",
            ".venv/",
            "env/",
            "ENV/",
            "*.egg",
            "*.egg-info/",
            "dist/",
            "build/",
            ".eggs/",
            "",
            "# Testing",
            ".pytest_cache/",
            ".coverage",
            "htmlcov/",
            "*.log",
            "",
            "# Environment",
            ".env",
            ".env.local",
        ]
    if frontend not in ("none", "") or language in ("javascript", "typescript"):
        lines += [
            "",
            "# Node.js",
            "node_modules/",
            ".npm",
            "npm-debug.log*",
            "yarn-error.log*",
            "",
            "# Build output",
            ".next/",
            "out/",
        ]
    lines += [
        "",
        "# IDE",
        ".idea/",
        ".vscode/",
        "*.swp",
        "*.swo",
        "",
        "# OS",
        ".DS_Store",
        "Thumbs.db",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# UI evidence: design reference download + implementation screenshot
# ---------------------------------------------------------------------------

def _find_chromium_binary() -> str:
    import shutil

    return (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or ""
    )


def _capture_browser_screenshot(url: str, out_path: str, log_fn) -> bool:
    chromium_bin = _find_chromium_binary()
    if not chromium_bin:
        log_fn("chromium not found — skipping browser screenshot")
        return False
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        result = subprocess.run(
            [
                chromium_bin,
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--virtual-time-budget=5000",
                f"--screenshot={out_path}",
                "--window-size=1440,1024",
                url,
            ],
            timeout=45,
            capture_output=True,
        )
        if result.returncode == 0 and os.path.isfile(out_path):
            return True
        stderr_text = (result.stderr or b"").decode(errors="replace")[:200]
        log_fn(f"Browser screenshot failed: {stderr_text or 'unknown error'}")
        return False
    except Exception as exc:
        log_fn(f"Browser screenshot error: {exc}")
        return False


def _get_design_reference_details(workspace: str) -> dict:
    """Return best-effort design reference inputs for screenshot capture."""
    details = {"thumbnail_url": "", "design_url": "", "local_design_ref": ""}
    if not workspace:
        return details

    # Priority: local reference screenshot saved by UI Design Agent
    local_ref = os.path.join(workspace, "ui-design", "design-reference.png")
    if os.path.isfile(local_ref):
        details["local_design_ref"] = local_ref

    design_context = _read_workspace_json(workspace, "team-lead/design-context.json")
    if design_context.get("url"):
        details["design_url"] = str(design_context.get("url", ""))

    stitch_path = os.path.join(workspace, "ui-design", "stitch-design.json")
    if not os.path.isfile(stitch_path):
        return details
    try:
        with open(stitch_path, encoding="utf-8") as fh:
            data = json.load(fh)
        # Only use imageUrls when this JSON came from a get_screen call (has screenId).
        # Project-level stitch data does NOT have screenId; its thumbnailScreenshot is the
        # project thumbnail (which may show a different screen — e.g. Practice Quiz instead
        # of Landing Page). Never use project-level thumbnails as design reference images.
        if data.get("screenId"):
            image_urls = data.get("imageUrls") or []
            if image_urls and image_urls[0]:
                details["thumbnail_url"] = image_urls[0]
    except Exception:
        pass
    return details


def _download_url_to_file(url: str, dest_path: str) -> bool:
    """Download a URL to a local file. Returns True on success."""
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=15) as resp:
            data = resp.read()
        with open(dest_path, "wb") as fh:
            fh.write(data)
        return os.path.getsize(dest_path) > 0
    except Exception as exc:
        print(f"[{AGENT_ID}] download failed ({url[:60]}): {exc}")
        return False


def _detect_ui_launch_plan(build_dir: str, analysis: dict, port: int) -> dict | None:
    """Return a best-effort local launch plan for taking UI screenshots."""
    frontend = str((analysis or {}).get("frontend_framework", "") or "").strip().lower()
    client_pkg_path = os.path.join(build_dir, "client", "package.json")
    root_pkg_path = os.path.join(build_dir, "package.json")
    candidate_specs = [
        ("client", _load_package_json(client_pkg_path), os.path.join(build_dir, "client")),
        ("root", _load_package_json(root_pkg_path), build_dir),
    ]

    def _candidate_urls(*ports: int) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for item_port in ports:
            for suffix in ("/", "/proj-4"):
                url = f"http://127.0.0.1:{item_port}{suffix}"
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
        return urls

    for label, manifest, cwd in candidate_specs:
        scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
        if not scripts:
            continue
        if "preview" in scripts:
            return {
                "label": f"{label}:preview",
                "cwd": cwd,
                "cmd": ["npm", "run", "preview", "--", "--host", "127.0.0.1", "--port", str(port)],
                "urls": _candidate_urls(port),
            }
        if "dev" in scripts:
            return {
                "label": f"{label}:dev",
                "cwd": cwd,
                "cmd": ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port)],
                "urls": _candidate_urls(port, 5173, 3000, 4173),
            }
        if "start" in scripts:
            return {
                "label": f"{label}:start",
                "cwd": cwd,
                "cmd": ["npm", "start"],
                "urls": _candidate_urls(port, 5173, 3000, 4173),
            }

    if frontend in {"react", "vue", "nextjs"} and (
        os.path.isfile(client_pkg_path) or os.path.isfile(root_pkg_path)
    ):
        return {
            "label": "fallback:node-ui",
            "cwd": build_dir,
            "cmd": ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port)],
            "urls": _candidate_urls(port, 5173, 3000, 4173),
        }
    return None


def _register_generated_artifact(
    clone_path: str,
    generated_files: list[dict],
    source_path: str,
    repo_rel_path: str,
    log_fn,
) -> bool:
    """Copy a generated artifact into the repo and register it for commit/PR evidence."""
    import shutil

    if not clone_path or not source_path or not os.path.isfile(source_path):
        return False

    normalized_rel_path = _normalize_plan_path(repo_rel_path)
    if not normalized_rel_path:
        return False

    dest_path = os.path.join(clone_path, normalized_rel_path)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if os.path.abspath(source_path) != os.path.abspath(dest_path):
        shutil.copy2(source_path, dest_path)
    if not any(_normalize_plan_path(item.get("path", "")) == normalized_rel_path for item in generated_files):
        generated_files.append({"path": normalized_rel_path, "content": "", "action": "create"})
    log_fn(f"Registered artifact in repo: {normalized_rel_path}")
    return True


def _register_runtime_repo_artifacts(
    clone_path: str,
    generated_files: list[dict],
    rel_dirs: list[str],
    log_fn,
) -> int:
    """Register runtime-generated files that already exist inside the cloned repo."""
    if not clone_path:
        return 0

    registered = 0
    for rel_dir in rel_dirs:
        normalized_rel_dir = _normalize_plan_path(rel_dir)
        if not normalized_rel_dir:
            continue
        artifact_dir = os.path.join(clone_path, normalized_rel_dir)
        if not os.path.isdir(artifact_dir):
            continue

        for root, _, files in os.walk(artifact_dir):
            for file_name in files:
                source_path = os.path.join(root, file_name)
                repo_rel_path = os.path.relpath(source_path, clone_path)
                if _register_generated_artifact(
                    clone_path,
                    generated_files,
                    source_path,
                    repo_rel_path,
                    log_fn,
                ):
                    registered += 1
    return registered


def _take_ui_screenshot(
    build_dir: str,
    python_executable: str,
    out_path: str,
    log_fn,
    analysis: dict | None = None,
    viewport: tuple = (1280, 900),
) -> bool:
    """
    Start the local UI app on a free port and take a headless chromium screenshot.
    Returns True if a screenshot was successfully saved to out_path.
    """
    import shutil
    import socket
    import time

    chromium_bin = _find_chromium_binary()
    if not chromium_bin:
        log_fn("chromium not found — skipping UI screenshot")
        return False

    # Find a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    launch_plan = _detect_ui_launch_plan(build_dir, analysis or {}, port)
    if launch_plan:
        env = {
            **os.environ,
            "CI": "true",
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "BROWSER": "none",
        }
        proc = None
        try:
            proc = subprocess.Popen(
                launch_plan["cmd"],
                cwd=launch_plan["cwd"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            started_url = ""
            for _ in range(40):
                time.sleep(0.5)
                if proc.poll() is not None:
                    break
                for url in launch_plan.get("urls", []):
                    try:
                        urlopen(url, timeout=2).read()
                        started_url = url
                        break
                    except Exception:
                        continue
                if started_url:
                    break

            if not started_url:
                log_fn(
                    f"UI app did not start for screenshot plan {launch_plan.get('label')} — skipping screenshot"
                )
                return False

            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            _w, _h = viewport
            result = subprocess.run(
                [
                    chromium_bin,
                    "--headless", "--no-sandbox", "--disable-gpu",
                    "--disable-dev-shm-usage",
                    f"--screenshot={out_path}",
                    f"--window-size={_w},{_h}",
                    started_url,
                ],
                timeout=30,
                capture_output=True,
            )
            if result.returncode == 0 and os.path.isfile(out_path):
                log_fn(f"Implementation screenshot saved ({os.path.getsize(out_path) // 1024} KB)")
                return True
            stderr_text = (result.stderr or b"").decode(errors="replace")[:200]
            log_fn(f"Screenshot failed: {stderr_text or 'unknown error'}")
            return False
        except Exception as exc:
            log_fn(f"Screenshot error: {exc}")
            return False
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

    # Auto-detect Flask app module for FLASK_APP env var
    flask_app_module = "app"
    for candidate_file, candidate_mod in [
        ("app/__init__.py", "app"),
        ("app.py", "app"),
        ("wsgi.py", "wsgi"),
    ]:
        fp = os.path.join(build_dir, candidate_file)
        if os.path.isfile(fp):
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if "create_app" in content:
                    flask_app_module = f"{candidate_mod}:create_app()"
                elif "Flask(" in content:
                    flask_app_module = candidate_mod
                break
            except Exception:
                pass

    env = {
        **os.environ,
        "FLASK_APP": flask_app_module,
        "FLASK_ENV": "testing",
        "FLASK_DEBUG": "0",
        "PORT": str(port),
    }
    url = f"http://127.0.0.1:{port}/"

    proc = None
    try:
        # Prefer `flask run` to avoid hard-coded ports in run.py
        proc = subprocess.Popen(
            [python_executable, "-m", "flask", "run",
             "--host=127.0.0.1", f"--port={port}", "--no-debugger"],
            cwd=build_dir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait up to 10 s for the app to respond
        started = False
        for _ in range(20):
            time.sleep(0.5)
            if proc.poll() is not None:
                break
            try:
                urlopen(url, timeout=2).read()
                started = True
                break
            except Exception:
                pass

        if not started:
            log_fn(f"Flask app did not start on port {port} — skipping screenshot")
            return False

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        _w, _h = viewport
        result = subprocess.run(
            [
                chromium_bin,
                "--headless", "--no-sandbox", "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--screenshot={out_path}",
                f"--window-size={_w},{_h}",
                url,
            ],
            timeout=30,
            capture_output=True,
        )
        if result.returncode == 0 and os.path.isfile(out_path):
            log_fn(f"Implementation screenshot saved ({os.path.getsize(out_path) // 1024} KB)")
            return True
        stderr_text = (result.stderr or b"").decode(errors="replace")[:200]
        log_fn(f"Screenshot failed: {stderr_text or 'unknown error'}")
        return False
    except Exception as exc:
        log_fn(f"Screenshot error: {exc}")
        return False
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def _run_workflow(task_id: str, message: dict):  # noqa: C901
    """
    Full Web Agent workflow running in a background thread.

    Phases:
      ANALYZING → PLANNING → IMPLEMENTING → PUSHING → COMPLETED
    """
    task = task_store.get(task_id)
    if not task:
        return

    metadata = message.get("metadata", {})
    compass_task_id = metadata.get("orchestratorTaskId", "")
    workflow_task_id = compass_task_id or task_id
    callback_url = metadata.get("orchestratorCallbackUrl", "")
    compass_url = metadata.get("compassUrl") or COMPASS_URL
    workspace = metadata.get("sharedWorkspacePath", "")
    permissions = metadata.get("permissions") if isinstance(metadata.get("permissions"), dict) else None
    acceptance_criteria: list = metadata.get("acceptanceCriteria") or []
    is_revision: bool = metadata.get("isRevision", False)
    review_issues: list = metadata.get("reviewIssues") or []
    tech_stack_constraints: dict = metadata.get("techStackConstraints") or {}
    # Design context passed from Team Lead (Stitch/Figma content + URL)
    design_context_meta: dict = metadata.get("designContext") or {}
    # Repo URL injected by Team Lead from Jira/analysis (preferred over text extraction)
    metadata_repo_url: str = metadata.get("targetRepoUrl", "")
    # Exit rule: how to shut down after task completion (defined by parent)
    exit_rule = PerTaskExitHandler.parse(metadata)

    task_instruction = _prepend_tech_stack_constraints(extract_text(message) or "", tech_stack_constraints)
    final_artifacts: list = []
    repo_url = metadata_repo_url or ""
    clone_path = ""
    branch_name = ""
    branch_kind = ""
    local_commit_sha = ""
    pr_url = ""
    build_dir = ""
    build_ok: bool | None = None
    agent_workspace = os.path.join(workspace, AGENT_ID) if workspace else ""
    runtime_config = {
        "runtime": summarize_runtime_configuration(),
        "rulesLoaded": bool(load_rules("web")),
        "workflowRulesLoaded": bool(load_rules("web", include_workflow=True)),
        "workflowInstructionsPresent": bool(metadata.get("devWorkflowInstructions")),
        "techStackConstraints": tech_stack_constraints,
        "skillPlaybooks": list(_DEVELOPMENT_SKILL_NAMES),
    }
    design_spec_for_audit = str(design_context_meta.get("content") or "")
    reference_html_for_audit = ""

    def log(phase: str):
        ts = local_clock_time()
        print(f"[{AGENT_ID}][{task_id}] [{ts}] {phase}")
        entry = f"[{ts}] {phase}"
        _append_workspace_file(workspace, f"{AGENT_ID}/command-log.txt", entry + "\n")
        _save_workspace_file(
            workspace,
            f"{AGENT_ID}/stage-summary.json",
            json.dumps(
                {
                    "taskId": workflow_task_id,
                    "agentTaskId": task_id,
                    "agentId": AGENT_ID,
                    "currentPhase": phase,
                    "repoUrl": repo_url,
                    "clonePath": clone_path,
                    "branch": branch_name,
                    "branchKind": branch_kind,
                    "localCommit": local_commit_sha,
                    "prUrl": pr_url,
                    "buildDir": build_dir,
                    "buildPassed": build_ok,
                    "acceptanceCriteria": acceptance_criteria,
                    "reviewIssues": review_issues,
                    "runtimeConfig": runtime_config,
                    "updatedAt": local_iso_timestamp(),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        _report_progress(compass_url, compass_task_id, phase)

    try:
        audit_log("TASK_STARTED", task_id=task_id, compass_task_id=compass_task_id)
        # If this is a revision, append review issues to instruction
        if is_revision and review_issues:
            issues_text = "\n".join(f"- {issue}" for issue in review_issues)
            task_instruction = (
                f"{task_instruction}\n\n"
                f"REVISION REQUEST — please fix the following issues:\n{issues_text}"
            )

        # Restore clone/branch state from prior task in the same workspace (revision)
        if is_revision and workspace:
            _ci = _read_workspace_json(workspace, f"{AGENT_ID}/clone-info.json")
            _bi = _read_workspace_json(workspace, f"{AGENT_ID}/branch-info.json")
            if _ci and _ci.get("clonePath") and os.path.isdir(_ci["clonePath"]):
                clone_path = _ci["clonePath"]
                repo_url = repo_url or _ci.get("repoUrl", "")
                log(f"Revision: reusing existing clone at {clone_path}")
            if _bi and _bi.get("branch"):
                branch_name = _bi["branch"]
                branch_kind = _bi.get("branchKind", "feature")
                pr_url = _bi.get("prUrl", "")
                log(f"Revision: restored branch={branch_name} pr={pr_url}")

        # ── Phase 1: Analyze ────────────────────────────────────────────────
        task_store.update_state(task_id, "ANALYZING", "Analyzing the web development task…")
        log("Analyzing task")
        analysis = _apply_tech_stack_constraints(
            _analyze_task(task_instruction, acceptance_criteria, repo_context=""),
            tech_stack_constraints,
        )
        log(
            f"Analysis: scope={analysis.get('scope')}, "
            f"frontend={analysis.get('frontend_framework')}, "
            f"backend={analysis.get('backend_framework')}, "
            f"ui={analysis.get('ui_library')}"
        )

        # ── Phase 2: Gather repo context (optional) ─────────────────────────
        task_store.update_state(task_id, "GATHERING_INFO", "Gathering context…")
        jira_content = ""
        repo_snapshot = ""

        if is_revision and review_issues:
            _save_workspace_file(
                workspace,
                f"{AGENT_ID}/review-notes.json",
                json.dumps(
                    {
                        "taskId": workflow_task_id,
                        "agentTaskId": task_id,
                        "agentId": AGENT_ID,
                        "isRevision": True,
                        "reviewIssues": review_issues,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

        # Extract Jira ticket key: prefer explicit metadata field from Team Lead,
        # then fall back to regex.  Require at least 2 digits to avoid matching
        # technical terms like "UTF-8", "ISO-8", "HTTP-2", etc.
        ticket_key, jira_content = _resolve_jira_context_from_metadata(task_instruction, metadata)
        if jira_content:
            log(f"Using Jira context from Team Lead metadata for {ticket_key or 'provided ticket'}")
            task_instruction = (
                f"{task_instruction}\n\n"
                f"Jira ticket context ({ticket_key or 'provided ticket'}):\n{jira_content[:3000]}"
            )
        elif ticket_key and workspace:
            log(f"Fetching Jira context for {ticket_key}")
            jira_content = _fetch_jira_context(
                task_id,
                ticket_key,
                workspace,
                compass_task_id,
                permissions=permissions,
            )
            if jira_content:
                log(f"Jira ticket {ticket_key} fetched ({len(jira_content)} chars)")
                # Enrich task instruction with Jira context
                task_instruction = (
                    f"{task_instruction}\n\n"
                    f"Jira ticket context ({ticket_key}):\n{jira_content[:3000]}"
                )

            # ── Dev Workflow Step 1: Mark ticket In Progress ─────────────────
            if not is_revision:
                log(f"Updating Jira ticket {ticket_key}: In Progress → assign self → comment")
                _jira_transition(
                    ticket_key,
                    "In Progress",
                    task_id,
                    workspace,
                    compass_task_id,
                    permissions=permissions,
                )
                _jira_assign_self(
                    ticket_key,
                    task_id,
                    workspace,
                    compass_task_id,
                    permissions=permissions,
                )
                _jira_add_comment(
                    ticket_key,
                    f"🤖 **Web Agent** (`{AGENT_ID}`) has picked up this ticket and started development.\n"
                    f"Internal task ID: `{workflow_task_id}`",
                    task_id,
                    workspace,
                    compass_task_id,
                    permissions=permissions,
                )
            else:
                rev_cycle = metadata.get("revisionCycle", 1)
                log(f"Revision {rev_cycle}: adding Jira progress comment for {ticket_key}")
                _jira_add_comment(
                    ticket_key,
                    f"📝 **Revision {rev_cycle}**: Applying code review feedback.\n"
                    f"Internal task ID: `{workflow_task_id}`",
                    task_id,
                    workspace,
                    compass_task_id,
                    permissions=permissions,
                )

        repo_url = metadata_repo_url or analysis.get("repo_url") or ""
        # Fall back to extracting from instruction text
        if not repo_url:
            url_match = re.search(r"https?://[^\s]+\.git", task_instruction) or \
                        re.search(r"https?://github\.com/[^\s]+", task_instruction) or \
                        re.search(r"https?://[^\s]*/scm/[^\s]+", task_instruction)
            if url_match:
                repo_url = url_match.group().rstrip("/.,;)")

        _require_shared_workspace_for_repo_task(repo_url, workspace)

        if repo_url and workspace:
            if clone_path:
                _ensure_clone_path_in_workspace(workspace, clone_path)
                # Revision: reuse existing clone, just refresh snapshot and re-analyse
                log(f"Revision: refreshing snapshot from existing clone {clone_path}")
                repo_snapshot = _read_repo_snapshot(clone_path)
                analysis = _apply_tech_stack_constraints(
                    _analyze_task(task_instruction, acceptance_criteria, repo_snapshot[:2000]),
                    tech_stack_constraints,
                )
            else:
                log(f"Cloning repository: {repo_url}")
                try:
                    clone_path = _clone_repo(
                        task_id,
                        repo_url,
                        workspace,
                        compass_task_id,
                        permissions=permissions,
                    )
                    _ensure_clone_path_in_workspace(workspace, clone_path)
                except Exception as err:
                    _save_workspace_file(
                        workspace,
                        f"{AGENT_ID}/clone-info.json",
                        json.dumps(
                            {
                                "taskId": workflow_task_id,
                                "agentTaskId": task_id,
                                "agentId": AGENT_ID,
                                "repoUrl": repo_url,
                                "clonePath": "",
                                "status": "failed",
                                "error": str(err),
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                    raise
                if clone_path:
                    log(f"Repository cloned to {clone_path}")
                    _save_workspace_file(
                        workspace,
                        f"{AGENT_ID}/clone-info.json",
                        json.dumps(
                            {
                                "taskId": workflow_task_id,
                                "agentTaskId": task_id,
                                "agentId": AGENT_ID,
                                "repoUrl": repo_url,
                                "clonePath": clone_path,
                                "status": "completed",
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                    repo_snapshot = _read_repo_snapshot(clone_path)
                    # Re-analyze with repo context
                    analysis = _apply_tech_stack_constraints(
                        _analyze_task(task_instruction, acceptance_criteria, repo_snapshot[:2000]),
                        tech_stack_constraints,
                    )

        # ── Phase 3: Plan ────────────────────────────────────────────────────
        task_store.update_state(task_id, "PLANNING", "Creating implementation plan…")
        log("Planning implementation")
        # Build design context string from metadata (passed by Team Lead)
        design_context_str = ""
        if design_context_meta:
            parts = []
            if design_context_meta.get("url"):
                parts.append(f"Design reference: {design_context_meta['url']}")
            if design_context_meta.get("page_name"):
                parts.append(f"Screen/Page: {design_context_meta['page_name']}")
            if design_context_meta.get("content"):
                parts.append(design_context_meta["content"])
            design_context_str = "\n".join(parts)
            log(f"Using design context from Team Lead ({len(design_context_str)} chars)")

        # Augment with local reference files saved by UI Design Agent (full spec + HTML template)
        if workspace:
            _stitch_local = _read_workspace_json(workspace, "ui-design/stitch-design.json")
            _local_code_html = _stitch_local.get("localCodeHtml", "")
            _local_design_md = _stitch_local.get("localDesignMd", "")
            if _local_code_html or _local_design_md:
                if _local_design_md:
                    design_spec_for_audit = _local_design_md
                if _local_code_html:
                    reference_html_for_audit = _local_code_html
                _extra: list[str] = []
                if _local_design_md:
                    _extra.append(
                        "## Design System Specification (DESIGN.md)\n"
                        "Follow this design spec exactly — colors, typography, spacing, components.\n"
                        + _local_design_md
                    )
                if _local_code_html:
                    _extra.append(
                        "## Reference HTML Implementation\n"
                        "This is the pixel-perfect reference implementation. "
                        "Use this as the authoritative design template — replicate the layout, "
                        "color scheme, typography, and component structure faithfully.\n"
                        f"```html\n{_local_code_html}\n```"
                    )
                design_context_str = "\n\n".join(filter(None, [design_context_str] + _extra))
                log(f"Design context enriched with local Stitch reference ({len(design_context_str)} chars)")

        plan = _plan_implementation(
            task_instruction,
            acceptance_criteria,
            analysis,
            repo_snapshot,
            design_context=design_context_str,
        )
        files_to_implement, removed_plan_files = _sanitize_plan_files(
            plan.get("files") or [],
            analysis,
            review_issues,
        )
        plan["files"] = files_to_implement
        if removed_plan_files:
            _save_workspace_file(
                workspace,
                f"{AGENT_ID}/plan-sanitization.json",
                json.dumps(
                    {
                        "taskId": workflow_task_id,
                        "agentTaskId": task_id,
                        "agentId": AGENT_ID,
                        "frontendFramework": analysis.get("frontend_framework"),
                        "removedFiles": removed_plan_files,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            removed_preview = ", ".join(item["path"] for item in removed_plan_files[:4])
            if len(removed_plan_files) > 4:
                removed_preview += ", ..."
            log(
                "Sanitized file plan — removed "
                f"{len(removed_plan_files)} conflicting/non-source file(s): {removed_preview}"
            )
        log(f"Plan ready — {len(files_to_implement)} file(s) to implement")

        # ── Auto-inject .gitignore if missing from plan and repo ─────────────
        _planned_paths_lower = {fi.get("path", "").lower() for fi in files_to_implement}
        if ".gitignore" not in _planned_paths_lower:
            _existing_gitignore = clone_path and os.path.isfile(os.path.join(clone_path, ".gitignore"))
            if not _existing_gitignore:
                gitignore_content = _generate_gitignore_content(analysis)
                files_to_implement.insert(0, {
                    "path": ".gitignore",
                    "action": "create",
                    "purpose": "Standard .gitignore for the project tech stack",
                    "key_logic": gitignore_content,
                    "content": gitignore_content,  # pre-generated — no LLM call needed
                    "dependencies": [],
                })
                plan["files"] = files_to_implement
                log("Auto-added .gitignore to plan")

        if not files_to_implement:
            raise RuntimeError("LLM returned an empty file plan — cannot proceed.")

        planned_paths = [file_info.get("path", "") for file_info in files_to_implement]
        if repo_url and clone_path:
            if not branch_name:
                branch_name, branch_kind = _select_branch_name(
                    task_instruction,
                    analysis,
                    planned_paths,
                    ticket_key,
                    task_id,
                    repo_url,
                    clone_path,
                    workspace,
                    compass_task_id,
                    permissions=permissions,
                )
            else:
                log(f"Revision: reusing branch {branch_name}")
            _checkout_local_branch(clone_path, branch_name, "main", log)
            _save_workspace_file(
                workspace,
                f"{AGENT_ID}/branch-info.json",
                json.dumps(
                    {
                        "taskId": workflow_task_id,
                        "agentTaskId": task_id,
                        "agentId": AGENT_ID,
                        "repoUrl": repo_url,
                        "clonePath": clone_path,
                        "branch": branch_name,
                        "branchKind": branch_kind,
                        "baseBranch": "main",
                        "localBranchPrepared": True,
                        "prUrl": pr_url,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

        # ── Phase 3b: Install plan dependencies at runtime ───────────────────
        install_deps = plan.get("install_dependencies") or []
        if install_deps:
            task_store.update_state(task_id, "INSTALLING_DEPS", "Installing runtime dependencies…")
            _install_plan_dependencies(install_deps, analysis.get("language", "python"), log)

        # ── Phase 4: Implement ───────────────────────────────────────────────
        task_store.update_state(task_id, "IMPLEMENTING", f"Implementing {len(files_to_implement)} file(s)…")
        log(f"Implementing {len(files_to_implement)} file(s)")

        generated_files: list[dict] = []
        context_summary = ""

        for i, file_info in enumerate(files_to_implement):
            file_path = file_info.get("path", f"file_{i}.txt")
            log(f"Generating [{i+1}/{len(files_to_implement)}]: {file_path}")

            # Read existing file if modifying and we have a clone
            existing_content = ""
            if file_info.get("action") == "modify" and clone_path:
                candidate = os.path.join(clone_path, file_path.lstrip("/"))
                if os.path.isfile(candidate):
                    try:
                        with open(candidate, encoding="utf-8", errors="replace") as fh:
                            existing_content = fh.read(8000)
                    except Exception:
                        pass

            # Use pre-generated content if available (e.g. auto-injected .gitignore)
            if file_info.get("content"):
                code = file_info["content"]
            else:
                code = _generate_file_code(
                    file_info,
                    task_instruction,
                    analysis,
                    context_summary,
                    existing_content,
                )
                # Strip any residual markdown fences from LLM output
                code = _strip_code_fences(code)

            generated_files.append({"path": file_path, "content": code, "action": file_info.get("action", "create")})

            # Update context summary for subsequent files (brief)
            context_summary += f"\n{file_path}: {file_info.get('purpose', '')}\n"
            if len(context_summary) > 2000:
                context_summary = context_summary[-2000:]

        log(f"Code generation complete — {len(generated_files)} file(s) ready")

        # ── Phase 5: Write to shared workspace ──────────────────────────────
        if clone_path:
            task_store.update_state(task_id, "WRITING", "Writing files into cloned repository…")
            written_clone_paths = _write_files_to_directory(clone_path, generated_files)
            log(f"Wrote {len(written_clone_paths)} file(s) into cloned repository")

        build_dir = clone_path or agent_workspace
        if build_dir and os.path.isdir(build_dir):
            _install_written_node_dependencies(build_dir, log)

        # ── Phase 5b: Build and test with LLM-guided recovery ───────────────
        build_ok = True  # default: assume passing if no build dir
        build_output = ""  # populated below if build/tests are actually run
        if build_dir and os.path.isdir(build_dir):
            task_store.update_state(task_id, "BUILDING", "Running build and tests…")
            log("Running build/tests")
            build_ok, build_output, build_attempts = _build_and_test_with_recovery(
                build_dir,
                task_instruction,
                analysis.get("language", "python"),
                log,
            )
            if build_ok:
                log("Build/tests passed")
            else:
                log(f"Build/tests could not be fully resolved: {build_output[:200]}")
            # Sync fixed files back to generated_files list
            for gf in generated_files:
                rel_path = gf["path"].lstrip("/")
                candidate = os.path.join(build_dir, rel_path)
                if os.path.isfile(candidate):
                    try:
                        with open(candidate, encoding="utf-8") as fh:
                            gf["content"] = fh.read()
                    except Exception:
                        pass
            _save_workspace_file(
                workspace,
                f"{AGENT_ID}/test-results.json",
                json.dumps(
                    {
                        "taskId": workflow_task_id,
                        "agentTaskId": task_id,
                        "agentId": AGENT_ID,
                        "buildDir": build_dir,
                        "passed": build_ok,
                        "attempts": build_attempts,
                        "finalOutput": build_output[:4000],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

        # ── Phase 5c: Capture UI evidence (design reference + screenshot) ────
        _screenshot_paths: list[str] = []
        _is_ui_task = (
            analysis.get("frontend_framework", "none") not in ("none", "")
            or any(fi.get("path", "").endswith(".html") for fi in generated_files)
            or analysis.get("scope") in ("frontend_only", "fullstack")
        )
        if _is_ui_task and workspace:
            evidence_dir = os.path.join(workspace, AGENT_ID)
            os.makedirs(evidence_dir, exist_ok=True)
            log("Capturing UI evidence (design reference + implementation screenshots)")
            design_ref_path = os.path.join(evidence_dir, "design-reference.png")

            # 1. Design reference — local file > Stitch thumbnail > browser capture
            design_reference = _get_design_reference_details(workspace)
            local_design_ref = design_reference.get("local_design_ref", "")
            thumbnail_url = design_reference.get("thumbnail_url", "")
            design_url = design_reference.get("design_url", "")
            design_saved = False
            if local_design_ref:
                import shutil as _shutil
                os.makedirs(os.path.dirname(design_ref_path), exist_ok=True)
                _shutil.copy2(local_design_ref, design_ref_path)
                log("Design reference screenshot copied from local reference")
                design_saved = True
            elif thumbnail_url:
                if _download_url_to_file(thumbnail_url, design_ref_path):
                    log("Design reference screenshot downloaded")
                    design_saved = True
                    _save_workspace_file(
                        workspace,
                        f"{AGENT_ID}/design-reference-url.txt",
                        thumbnail_url,
                    )
                else:
                    log(f"Could not download design reference — URL saved as text")
                    _save_workspace_file(
                        workspace,
                        f"{AGENT_ID}/design-reference-url.txt",
                        thumbnail_url,
                    )

            if not design_saved and design_url:
                if _capture_browser_screenshot(design_url, design_ref_path, log):
                    log("Design reference screenshot captured from design URL")
                _save_workspace_file(
                    workspace,
                    f"{AGENT_ID}/design-reference-url.txt",
                    design_url,
                )

            # 2. Implementation screenshots — one per viewport (best-effort)
            # Files are named screenshot-{W}x{H}.png — no platform labels.
            _captured: dict[tuple[int, int], str] = {}  # viewport → local path
            if build_dir:
                py_exec = _ensure_local_python_env(
                    build_dir, analysis.get("language", "python"), log
                )
                for _vp in _UI_SCREENSHOT_VIEWPORTS:
                    _vw, _vh = _vp
                    _out = os.path.join(evidence_dir, f"screenshot-{_vw}x{_vh}.png")
                    if _take_ui_screenshot(build_dir, py_exec, _out, log, analysis=analysis, viewport=_vp):
                        _captured[_vp] = _out
                        # Replace LLM-generated placeholder if first (widest) viewport
                        if not _captured or _vp == _UI_SCREENSHOT_VIEWPORTS[0]:
                            import shutil as _shutil
                            for _rel in ("work/screenshots/index.png", ".work/screenshots/index.png"):
                                _placeholder = os.path.join(build_dir, _rel)
                                if os.path.exists(_placeholder):
                                    os.makedirs(os.path.dirname(_placeholder), exist_ok=True)
                                    _shutil.copy2(_out, _placeholder)
                                    log(f"Replaced placeholder screenshot at {_rel}")

            if clone_path:
                if os.path.isfile(design_ref_path):
                    _register_generated_artifact(
                        clone_path,
                        generated_files,
                        design_ref_path,
                        "docs/evidence/design-reference.png",
                        log,
                    )
                for (_vw, _vh), _src in _captured.items():
                    _register_generated_artifact(
                        clone_path,
                        generated_files,
                        _src,
                        f"docs/evidence/screenshot-{_vw}x{_vh}.png",
                        log,
                    )
                _register_runtime_repo_artifacts(
                    clone_path,
                    generated_files,
                    ["artifacts/figma"],
                    log,
                )
            _screenshot_paths = list(_captured.values()) if _is_ui_task else []

        # ── Phase 5d: Self-assessment loop (up to 5 iterations) ─────────────
        MAX_SELF_IMPROVE = 5
        for _sa_iter in range(MAX_SELF_IMPROVE):
            log(f"Self-assess [{_sa_iter + 1}/{MAX_SELF_IMPROVE}]")
            task_store.update_state(
                task_id, "SELF_ASSESSING",
                f"Self-assessment [{_sa_iter + 1}/{MAX_SELF_IMPROVE}]…",
            )
            _design_compare = _compare_design_fidelity(
                generated_files,
                build_ok,
                build_output,
                design_spec_for_audit,
                reference_html_for_audit,
                _screenshot_paths,
            )
            _design_missing = _design_compare.get("missing") or []
            _design_redundant = _design_compare.get("redundant") or []
            _design_wrong = _design_compare.get("wrong") or []
            _design_findings = _design_missing + _design_redundant + _design_wrong
            _design_files = [
                item.get("file_to_fix", "") for item in _design_findings if item.get("file_to_fix")
            ]
            _save_workspace_file(
                workspace,
                f"{AGENT_ID}/design-compare-{_sa_iter + 1}.json",
                json.dumps(
                    {
                        "iteration": _sa_iter + 1,
                        "fidelityScore": _design_compare.get("fidelity_score", 0),
                        "implemented": _design_compare.get("implemented") or [],
                        "missing": _design_missing,
                        "redundant": _design_redundant,
                        "wrong": _design_wrong,
                        "summary": _design_compare.get("summary", ""),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            _assess = _self_assess_implementation(
                task_instruction,
                acceptance_criteria,
                generated_files,
                build_ok,
                build_output,
                _screenshot_paths,
            )
            _assess_passed = bool(_assess.get("passed")) and not _design_findings
            _assess_issues = list(_assess.get("issues") or [])
            for _finding in _design_findings:
                _file_to_fix = _finding.get("file_to_fix") or "unknown-file"
                _severity = _finding.get("severity", "major")
                _requirement = _finding.get("requirement", "Unspecified design issue")
                _fix_hint = _finding.get("fix_hint", "Fix the design mismatch.")
                _assess_issues.append(
                    f"{_file_to_fix}: {_severity}: {_requirement} — {_fix_hint}"
                )
            _assess_issues = list(dict.fromkeys(_assess_issues))
            _assess_files = list(dict.fromkeys((_assess.get("files_to_fix") or []) + _design_files))
            log(
                f"Self-assess [{_sa_iter + 1}/{MAX_SELF_IMPROVE}]: "
                f"passed={_assess_passed}, issues={len(_assess_issues)}, "
                f"design_score={_design_compare.get('fidelity_score', 0)}"
            )
            _save_workspace_file(
                workspace,
                f"{AGENT_ID}/self-assess-{_sa_iter + 1}.json",
                json.dumps(
                    {
                        "iteration": _sa_iter + 1,
                        "passed": _assess_passed,
                        "issues": _assess_issues,
                        "filesToFix": _assess_files,
                        "summary": _assess.get("summary", ""),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            if _assess_passed or _sa_iter == MAX_SELF_IMPROVE - 1:
                if not _assess_passed:
                    log("Self-assess: max iterations reached, proceeding with current implementation")
                break
            if not _assess_files:
                log("Self-assess: issues found but no specific files to fix — proceeding")
                break
            log(f"Self-assess: re-generating {len(_assess_files)} file(s): {_assess_files}")
            _fix_context = ""
            _file_issue_map: dict[str, list[str]] = {}
            for _issue in _assess_issues:
                for _target_file in _assess_files:
                    if _target_file and _target_file in _issue:
                        _file_issue_map.setdefault(_target_file, []).append(_issue)
            for _fi in files_to_implement:
                if _fi.get("path") not in _assess_files:
                    continue
                _existing_fix = ""
                if clone_path:
                    _fp = os.path.join(clone_path, _fi["path"].lstrip("/"))
                    if os.path.isfile(_fp):
                        try:
                            with open(_fp, encoding="utf-8", errors="replace") as _fh:
                                _existing_fix = _fh.read(8000)
                        except Exception:
                            pass
                _targeted_instruction = task_instruction
                _file_issues = _file_issue_map.get(_fi["path"], [])
                if _file_issues:
                    _targeted_instruction += "\n\nFix these specific review findings for this file:\n"
                    _targeted_instruction += "\n".join(f"- {_issue}" for _issue in _file_issues)
                _new_code = _generate_file_code(
                    _fi, _targeted_instruction, analysis, _fix_context, _existing_fix,
                )
                _new_code = _strip_code_fences(_new_code)
                for _gf in generated_files:
                    if _gf["path"] == _fi["path"]:
                        _gf["content"] = _new_code
                        break
                else:
                    generated_files.append({
                        "path": _fi["path"], "content": _new_code,
                        "action": _fi.get("action", "modify"),
                    })
                _fix_context += f"\n{_fi['path']}: {_fi.get('purpose', '')}\n"
                if _file_issues:
                    _fix_context += "Review findings:\n" + "\n".join(f"- {_issue}" for _issue in _file_issues) + "\n"
            if clone_path:
                _fixed_subset = [gf for gf in generated_files if gf["path"] in _assess_files]
                _written_fix = _write_files_to_directory(clone_path, _fixed_subset)
                log(f"Self-assess: wrote {len(_written_fix)} fixed file(s)")
                if build_dir and os.path.isdir(build_dir):
                    build_ok, build_output, _ = _build_and_test_with_recovery(
                        build_dir, task_instruction, analysis.get("language", "python"), log,
                    )
                    log(f"Self-assess: rebuild {'passed' if build_ok else 'failed'}")
                    for _gf in generated_files:
                        _rel = _gf["path"].lstrip("/")
                        _cand = os.path.join(build_dir, _rel)
                        if os.path.isfile(_cand):
                            try:
                                with open(_cand, encoding="utf-8") as _fh:
                                    _gf["content"] = _fh.read()
                            except Exception:
                                pass
                if _is_ui_task and build_dir and workspace:
                    _py_exec2 = _ensure_local_python_env(
                        build_dir, analysis.get("language", "python"), log,
                    )
                    _captured2: dict = {}
                    for _vp2 in _UI_SCREENSHOT_VIEWPORTS:
                        _vw2, _vh2 = _vp2
                        _out2 = os.path.join(workspace, AGENT_ID, f"screenshot-{_vw2}x{_vh2}.png")
                        if _take_ui_screenshot(
                            build_dir, _py_exec2, _out2, log, analysis=analysis, viewport=_vp2,
                        ):
                            _captured2[_vp2] = _out2
                    if _captured2:
                        _screenshot_paths = list(_captured2.values())
                        log(f"Self-assess: refreshed {len(_screenshot_paths)} screenshot(s)")

        if clone_path and branch_name:
            if not branch_kind:
                branch_kind = "feature"
            if branch_kind == "hotfix":
                commit_msg = f"fix({ticket_key}): web agent implementation"
            elif branch_kind == "chore":
                commit_msg = f"chore({workflow_task_id}): docs and tests update"
            else:
                commit_msg = f"feat({ticket_key}): web agent implementation" if ticket_key else f"feat({workflow_task_id}): web agent implementation"
            local_commit_sha = _commit_local_changes(
                clone_path,
                branch_name,
                generated_files,
                commit_msg,
                log,
            )

        # ── Phase 6: Push files and create PR (if repo available) ────────────
        if repo_url and workspace:
            task_store.update_state(task_id, "PUSHING", "Creating branch and pushing code…")
            base_branch = "main"  # PRs always target the default branch
            branch_created = bool(branch_name)
            _save_workspace_file(
                workspace,
                f"{AGENT_ID}/branch-info.json",
                json.dumps(
                    {
                        "taskId": workflow_task_id,
                        "agentTaskId": task_id,
                        "agentId": AGENT_ID,
                        "repoUrl": repo_url,
                        "clonePath": clone_path,
                        "branch": branch_name,
                        "branchKind": branch_kind,
                        "baseBranch": base_branch,
                        "localBranchPrepared": branch_created,
                        "localCommit": local_commit_sha,
                        "prUrl": pr_url,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            if not branch_created:
                log("Warning: could not prepare a local branch — files saved to cloned repo only")
            else:
                # Push generated files
                push_files = [
                    {"path": gf["path"], "content": gf["content"]}
                    for gf in generated_files
                ]
                pushed = _push_files(
                    task_id,
                    repo_url,
                    branch_name,
                    push_files,
                    commit_msg,
                    workspace,
                    compass_task_id,
                    permissions=permissions,
                    base_branch=base_branch,
                )
                if pushed:
                    log("Files pushed to branch")
                    # Create PR
                    files_changed = [gf["path"] for gf in generated_files]
                    pr_title, pr_body = _generate_pr_description(
                        task_instruction,
                        acceptance_criteria,
                        files_changed,
                        plan.get("plan_summary") or "Web agent implementation",
                        design_context_meta=design_context_meta,
                        test_output=build_output if build_dir else "",
                        repo_url=repo_url or "",
                        branch_name=branch_name or "",
                    )
                    _save_pr_evidence(
                        workspace,
                        taskId=workflow_task_id,
                        agentTaskId=task_id,
                        repoUrl=repo_url,
                        clonePath=clone_path,
                        branch=branch_name,
                        branchKind=branch_kind,
                        localCommit=local_commit_sha,
                        baseBranch=base_branch,
                        title=pr_title,
                        body=pr_body,
                        buildPassed=build_ok,
                        generatedFiles=files_changed,
                    )
                    if is_revision and pr_url:
                        log(f"Revision: pushing to existing PR {pr_url}")
                    else:
                        pr_url = _create_pr(
                            task_id,
                            repo_url,
                            branch_name,
                            base_branch,
                            pr_title,
                            pr_body,
                            workspace,
                            compass_task_id,
                            permissions=permissions,
                        )
                    _save_pr_evidence(
                        workspace,
                        taskId=workflow_task_id,
                        agentTaskId=task_id,
                        repoUrl=repo_url,
                        clonePath=clone_path,
                        branch=branch_name,
                        branchKind=branch_kind,
                        localCommit=local_commit_sha,
                        baseBranch=base_branch,
                        title=pr_title,
                        body=pr_body,
                        url=pr_url,
                        buildPassed=build_ok,
                        generatedFiles=files_changed,
                    )
                    _save_workspace_file(
                        workspace,
                        f"{AGENT_ID}/branch-info.json",
                        json.dumps(
                            {
                                "taskId": workflow_task_id,
                                "agentTaskId": task_id,
                                "agentId": AGENT_ID,
                                "repoUrl": repo_url,
                                "clonePath": clone_path,
                                "branch": branch_name,
                                "branchKind": branch_kind,
                                "baseBranch": base_branch,
                                "localBranchPrepared": branch_created,
                                "localCommit": local_commit_sha,
                                "prUrl": pr_url,
                                "buildPassed": build_ok,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                    if pr_url:
                        log(f"{'PR updated' if is_revision else 'PR created'}: {pr_url}")
                        # ── Dev Workflow Step 2: Update Jira after PR ────────
                        if ticket_key:
                            if not is_revision:
                                _jira_transition(
                                    ticket_key,
                                    "In Review",
                                    task_id,
                                    workspace,
                                    compass_task_id,
                                    permissions=permissions,
                                )
                            test_status = "✅ Build/tests passed" if build_ok else "⚠️ Build/tests had issues"
                            _jira_add_comment(
                                ticket_key,
                                "",
                                task_id,
                                workspace,
                                compass_task_id,
                                permissions=permissions,
                                adf_body=_build_pr_jira_comment_adf(
                                    pr_url,
                                    branch_name,
                                    test_status,
                                    generated_files,
                                    plan.get("plan_summary", "Implementation complete."),
                                ),
                                comment_preview=(
                                    f"PR: {pr_url}\n"
                                    f"Branch: {branch_name}\n"
                                    f"Test Status: {test_status}"
                                ),
                            )
                    else:
                        log("Warning: PR creation returned no URL")
                else:
                    log("Warning: file push failed — files saved to workspace only")

        # ── Phase 7: Build artifacts and finalize ────────────────────────────
        task_store.update_state(task_id, "COMPLETING", "Finalizing…")

        files_list = [gf["path"] for gf in generated_files]
        summary = _generate_summary(task_instruction, acceptance_criteria, files_list, pr_url)
        log(f"Summary: {summary[:120]}")
        _save_workspace_file(workspace, f"{AGENT_ID}/final-summary.md", summary)

        # Create artifacts: one per generated file + summary
        summary_artifact = build_text_artifact(
            "web-agent-summary",
            summary,
            metadata={
                "agentId": AGENT_ID,
                "capability": "web.task.execute",
                "orchestratorTaskId": compass_task_id,
                "taskId": workflow_task_id,
                "agentTaskId": task_id,
                "prUrl": pr_url,
                "url": pr_url,      # alias used by Compass evidence extraction
                "branch": branch_name,
                "filesCount": len(generated_files),
                # jiraInReview is read by Compass to display "Completed / In Review"
                # without having to scan the shared workspace filesystem.
                "jiraInReview": bool(pr_url and ticket_key),
            },
        )
        final_artifacts = [summary_artifact]

        for artifact_name, artifact_path in (
            ("web-agent-clone-info", f"{AGENT_ID}/clone-info.json"),
            ("web-agent-branch-info", f"{AGENT_ID}/branch-info.json"),
            ("web-agent-test-results", f"{AGENT_ID}/test-results.json"),
            ("web-agent-jira-actions", f"{AGENT_ID}/jira-actions.json"),
            ("web-agent-pr-evidence", f"{AGENT_ID}/pr-evidence.json"),
        ):
            payload = _read_workspace_json(workspace, artifact_path)
            if payload:
                final_artifacts.append(
                    build_text_artifact(
                        artifact_name,
                        json.dumps(payload, ensure_ascii=False, indent=2)[:3000],
                        artifact_type="application/json",
                        metadata={
                            "agentId": AGENT_ID,
                            "capability": "web.task.execute",
                            "orchestratorTaskId": compass_task_id,
                            "path": artifact_path,
                        },
                    )
                )

        # Add code file artifacts (truncated for artifact payload)
        for gf in generated_files[:10]:
            code_preview = gf["content"][:2000] + ("\n...[truncated]" if len(gf["content"]) > 2000 else "")
            final_artifacts.append(
                build_text_artifact(
                    f"web-agent-file-{gf['path'].replace('/', '-')}",
                    f"File: {gf['path']}\n\n{code_preview}",
                    metadata={
                        "agentId": AGENT_ID,
                        "capability": "web.task.execute",
                        "orchestratorTaskId": compass_task_id,
                        "filePath": gf["path"],
                    },
                )
            )

        task_store.update_state(task_id, "TASK_STATE_COMPLETED", summary)
        task = task_store.get(task_id)
        if task:
            task.artifacts = final_artifacts

        log("Task completed successfully")
        audit_log(
            "TASK_COMPLETED",
            task_id=task_id,
            compass_task_id=compass_task_id,
            files_count=len(generated_files),
            pr_url=pr_url,
        )
        _notify_callback(callback_url, task_id, "TASK_STATE_COMPLETED", summary, final_artifacts)

    except Exception as err:
        error_text = str(err)
        failure_artifacts = []
        if isinstance(err, PermissionEscalationRequired):
            failure_artifacts = [build_permission_denied_artifact(err.details, agent_id=AGENT_ID)]
        print(f"[{AGENT_ID}][{task_id}] FAILED: {error_text}")
        task_store.update_state(task_id, "TASK_STATE_FAILED", f"Web Agent failed: {error_text[:500]}")
        task = task_store.get(task_id)
        if task:
            task.artifacts = failure_artifacts
        _save_workspace_file(
            workspace,
            f"{AGENT_ID}/review-notes.json",
            json.dumps(
                {
                    "taskId": workflow_task_id,
                    "agentTaskId": task_id,
                    "agentId": AGENT_ID,
                    "error": error_text,
                    "reviewIssues": review_issues,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        audit_log("TASK_FAILED", task_id=task_id, error=error_text[:300])
        _notify_callback(
            callback_url, task_id, "TASK_STATE_FAILED",
            f"Web Agent failed: {error_text[:500]}", failure_artifacts
        )
    finally:
        _apply_task_exit_rule(task_id, exit_rule)


def _strip_code_fences(code: str) -> str:
    """Remove markdown code fences from LLM output."""
    code = code.strip()
    if code.startswith("```"):
        lines = code.splitlines()
        start = 1
        end = len(lines)
        while end > start and lines[end - 1].strip() in ("```", ""):
            end -= 1
        code = "\n".join(lines[start:end])
    return code


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class WebAgentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": AGENT_ID})
            return

        if path == "/.well-known/agent-card.json":
            with open(_AGENT_CARD_PATH, encoding="utf-8") as fh:
                card = json.load(fh)
            text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
            self._send_json(200, json.loads(text))
            return

        m = re.fullmatch(r"/tasks/([^/]+)", path)
        if m:
            task = task_store.get(m.group(1))
            if task:
                self._send_json(200, {"task": task.to_dict()})
            else:
                self._send_json(404, {"error": "task_not_found"})
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path

        # POST /tasks/{id}/ack — parent confirms it received our callback
        m_ack = re.fullmatch(r"/tasks/([^/]+)/ack", path)
        if m_ack:
            task_id = m_ack.group(1)
            acked = exit_handler.acknowledge(task_id)
            print(f"[{AGENT_ID}] Received ACK for task {task_id} (registered={acked})")
            self._send_json(200, {"ok": True, "task_id": task_id})
            return

        if path != "/message:send":
            self._send_json(404, {"error": "not_found"})
            return

        body = self._read_body()
        message = body.get("message", {})
        if not message:
            self._send_json(400, {"error": "missing_message"})
            return

        task = task_store.create()
        audit_log(
            "TASK_RECEIVED",
            task_id=task.task_id,
            instruction_preview=extract_text(message)[:120],
        )

        worker = threading.Thread(
            target=_run_workflow,
            args=(task.task_id, message),
            daemon=True,
        )
        worker.start()

        self._send_json(200, {"task": task.to_dict()})

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if any(p in line for p in ("/health", "/.well-known/agent-card.json")):
            return
        print(
            f"[{AGENT_ID}] {line} "
            f"{args[1] if len(args) > 1 else ''} "
            f"{args[2] if len(args) > 2 else ''}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_SERVER: ThreadingHTTPServer | None = None


def _schedule_shutdown(delay_seconds: int = 5):
    def _do_shutdown():
        time.sleep(delay_seconds)
        print(f"[{AGENT_ID}] Per-task shutdown triggered")
        if _SERVER:
            _SERVER.shutdown()

    threading.Thread(target=_do_shutdown, daemon=True).start()

def main():
    global _SERVER
    print(f"[{AGENT_ID}] Web Agent starting on {HOST}:{PORT}")
    agent_directory.start()
    _SERVER = ThreadingHTTPServer((HOST, PORT), WebAgentHandler)
    reporter.start()
    _SERVER.serve_forever()


if __name__ == "__main__":
    main()
