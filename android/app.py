"""Android Agent — Android development execution agent.

Capabilities:
- Native Android: Kotlin, Java, Jetpack Compose, XML layouts
- Gradle build system configuration
- Analyzes task, generates code, writes files to shared workspace
- Creates feature branch and pull request via SCM Agent
- Queries Jira Agent for ticket context, UI Design Agent for design references
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
from android import prompts

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
AGENT_ID = os.environ.get("AGENT_ID", "android-agent")
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{AGENT_ID}-local")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://android-agent:{PORT}")
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
COMPASS_URL = os.environ.get("COMPASS_URL", "http://compass:8080")

ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))
TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "600"))
SYNC_AGENT_TIMEOUT = int(os.environ.get("SYNC_AGENT_TIMEOUT_SECONDS", "120"))
MAX_BUILD_RETRIES = int(os.environ.get("ANDROID_MAX_BUILD_RETRIES", "3"))

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


def _resolve_jira_context_from_metadata(
    task_instruction: str,
    metadata: dict | None = None,
) -> tuple[str, str]:
    metadata = metadata or {}
    jira_context = metadata.get("jiraContext")
    if not isinstance(jira_context, dict):
        jira_context = {}

    ticket_key = str(jira_context.get("ticketKey") or metadata.get("jiraTicketKey") or "").strip()
    if not ticket_key:
        ticket_match = re.search(r"\b([A-Z][A-Z0-9]+-\d{2,})\b", task_instruction or "")
        if ticket_match:
            ticket_key = ticket_match.group(1)

    content = str(jira_context.get("content") or "").strip()
    return ticket_key, content


def _append_workspace_event(workspace_path: str, relative_name: str, event: dict) -> None:
    payload = _read_workspace_json(workspace_path, relative_name)
    raw_events = payload.get("events")
    events = raw_events if isinstance(raw_events, list) else []
    events.append(event)
    payload["events"] = events
    _save_workspace_file(
        workspace_path,
        relative_name,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


# ---------------------------------------------------------------------------
# A2A and boundary-agent helpers
# ---------------------------------------------------------------------------

def _resolve_agent_service_url(capability: str) -> str:
    try:
        _, instance = agent_directory.resolve_capability(capability)
    except RegistryUnavailableError as err:
        raise RuntimeError(
            f"Registry unavailable while resolving '{capability}': {err}"
        ) from err
    except CapabilityUnavailableError as err:
        raise RuntimeError(
            f"Required capability '{capability}' is unavailable."
        ) from err
    service_url = (instance or {}).get("service_url", "")
    if not service_url:
        raise RuntimeError(f"Capability '{capability}' has no routable service URL.")
    return service_url.rstrip("/")


def _a2a_send(agent_url: str, message: dict) -> dict:
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
    agent_url = _resolve_agent_service_url(capability)
    message = {
        "messageId": f"android-{task_id}-{capability}-{int(time.time())}",
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


def _jira_request_json(
    capability: str,
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    workspace: str = "",
    task_id: str = "",
    compass_task_id: str = "",
    timeout: int = 30,
    permissions: dict | None = None,
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


def _notify_callback(
    callback_url: str,
    task_id: str,
    state: str,
    status_message: str,
    artifacts: list | None = None,
):
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


def _apply_task_exit_rule(task_id: str, exit_rule: dict) -> None:
    def _run():
        rule_type = (exit_rule or {}).get("type", "wait_for_parent_ack")
        if rule_type == "auto_stop":
            if os.environ.get("AUTO_STOP_AFTER_TASK", "").strip() != "1":
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
# Jira helpers (via boundary Jira Agent)
# ---------------------------------------------------------------------------

def _fetch_jira_context(task_id: str, ticket_key: str, workspace: str, compass_task_id: str,
                        permissions: dict | None = None) -> str:
    try:
        result = _jira_request_json(
            "jira.ticket.fetch",
            "GET",
            f"/jira/tickets/{ticket_key}",
            workspace=workspace,
            task_id=task_id,
            compass_task_id=compass_task_id,
            permissions=permissions,
        )
        issue = result.get("issue") or {}
        content = json.dumps(issue, ensure_ascii=False, indent=2) if issue else ""
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "fetch", "completed",
                            agent_task_id=task_id, contentLength=len(content))
        return content
    except Exception as err:
        print(f"[{AGENT_ID}] Could not fetch Jira ticket {ticket_key}: {err}")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "fetch", "failed",
                            agent_task_id=task_id, error=str(err))
        return ""


def _jira_transition(ticket_key: str, target_status: str, task_id: str, workspace: str, compass_task_id: str,
                     permissions: dict | None = None):
    try:
        _jira_request_json(
            "jira.ticket.transition",
            "POST",
            f"/jira/transitions/{ticket_key}",
            payload={"transition": target_status},
            workspace=workspace,
            task_id=task_id,
            compass_task_id=compass_task_id,
            permissions=permissions,
        )
        print(f"[{AGENT_ID}] Jira {ticket_key} transitioned to '{target_status}'")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "transition", "completed",
                            agent_task_id=task_id, targetStatus=target_status)
    except Exception as err:
        print(f"[{AGENT_ID}] Jira transition failed (non-critical): {err}")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "transition", "failed",
                            agent_task_id=task_id, targetStatus=target_status, error=str(err))


def _jira_assign_self(ticket_key: str, task_id: str, workspace: str, compass_task_id: str,
                      permissions: dict | None = None):
    try:
        response = _jira_request_json(
            "jira.user.myself",
            "GET",
            "/jira/myself",
            workspace=workspace,
            task_id=task_id,
            compass_task_id=compass_task_id,
            permissions=permissions,
        )
        user = response.get("user") or {}
        account_id = user.get("accountId") or ""
        if not account_id:
            return
        _jira_request_json(
            "jira.ticket.assignee",
            "PUT",
            f"/jira/assignee/{ticket_key}",
            payload={"accountId": account_id},
            workspace=workspace,
            task_id=task_id,
            compass_task_id=compass_task_id,
            permissions=permissions,
        )
        print(f"[{AGENT_ID}] Jira {ticket_key} assigned to service account")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "assign", "completed",
                            agent_task_id=task_id, accountId=account_id)
    except Exception as err:
        print(f"[{AGENT_ID}] Jira assign failed (non-critical): {err}")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "assign", "failed",
                            agent_task_id=task_id, error=str(err))


def _jira_add_comment(ticket_key: str, comment: str, task_id: str, workspace: str, compass_task_id: str,
                      permissions: dict | None = None):
    try:
        _jira_request_json(
            "jira.comment.add",
            "POST",
            f"/jira/comments/{ticket_key}",
            payload={"text": comment},
            workspace=workspace,
            task_id=task_id,
            compass_task_id=compass_task_id,
            permissions=permissions,
        )
        print(f"[{AGENT_ID}] Jira {ticket_key} comment added")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "comment", "completed",
                            agent_task_id=task_id, commentPreview=comment[:240])
    except Exception as err:
        print(f"[{AGENT_ID}] Jira comment failed (non-critical): {err}")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "comment", "failed",
                            agent_task_id=task_id, error=str(err))


def _record_jira_action(workspace_path, workflow_task_id, ticket_key, action, status, **details):
    event = {
        "ts": local_iso_timestamp(),
        "taskId": workflow_task_id,
        "agentId": AGENT_ID,
        "ticketKey": ticket_key,
        "action": action,
        "status": status,
    }
    event.update({k: v for k, v in details.items() if v not in (None, "")})
    _append_workspace_event(workspace_path, f"{AGENT_ID}/jira-actions.json", event)


# ---------------------------------------------------------------------------
# SCM helpers (via boundary SCM Agent)
# ---------------------------------------------------------------------------

def _clone_repo(task_id: str, repo_url: str, workspace: str, compass_task_id: str,
                permissions: dict | None = None) -> str:
    result = _call_sync_agent(
        "scm.git.clone",
        f"Clone repository {repo_url} to {workspace}",
        task_id,
        workspace,
        compass_task_id,
        permissions=permissions,
    )
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
    extra = result.get("extra", {})
    if extra.get("clonePath"):
        return extra["clonePath"]
    state = result.get("status", {}).get("state", "")
    status_text = extract_text(result.get("status", {}).get("message", {})).strip()
    if state in {"TASK_STATE_FAILED", "FAILED"}:
        raise RuntimeError(status_text or f"SCM clone failed for {repo_url}")
    raise RuntimeError(f"SCM clone did not return a clone path for {repo_url}")


def _list_remote_branches(task_id: str, repo_url: str, workspace: str, compass_task_id: str,
                          permissions: dict | None = None) -> set:
    """Return the set of remote branch names from the SCM agent (best-effort)."""
    try:
        result = _call_sync_agent(
            "scm.branch.list",
            f"List branches in {repo_url}",
            task_id,
            workspace,
            compass_task_id,
            permissions=permissions,
        )
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
                    str(b.get("name", "")).strip()
                    for b in branches
                    if isinstance(b, dict) and str(b.get("name", "")).strip()
                }
    except Exception as err:
        print(f"[{AGENT_ID}] Could not list remote branches (non-critical): {err}")
    return set()


def _unique_branch_name(base: str, remote_branches: set) -> str:
    """Return *base* if it is not taken, otherwise *base*_2, *base*_3, …"""
    if base not in remote_branches:
        return base
    for n in range(2, 100):
        candidate = f"{base}_{n}"
        if candidate not in remote_branches:
            return candidate
    raise RuntimeError(f"Could not allocate a unique branch name for {base}")


def _create_branch(task_id: str, repo_url: str, branch_name: str, base_branch: str,
                    workspace: str, compass_task_id: str,
                    permissions: dict | None = None) -> bool:
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
    base_branch: str = "main",
    permissions: dict | None = None,
) -> bool:
    # Extract owner/repo for structured payload
    owner, repo = "", ""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s?#]+)", repo_url or "")
    if m:
        owner = m.group(1)
        repo = m.group(2).rstrip(".git")
    # Bitbucket Server URL pattern
    if not owner:
        m = re.search(r"/projects/([^/]+)/repos/([^/\s?#]+)", repo_url or "")
        if m:
            owner = m.group(1)
            repo = m.group(2)
    try:
        scm_service_url = _resolve_agent_service_url("scm.git.push")
        message = {
            "messageId": f"android-{task_id}-push-{int(time.time())}",
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
    safe_base = _sanitize_base_branch(base_branch)
    owner, repo = "", ""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s?#]+)", repo_url or "")
    if m:
        owner = m.group(1)
        repo = m.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
    if not owner:
        m = re.search(r"/projects/([^/]+)/repos/([^/\s?#]+)", repo_url or "")
        if m:
            owner = m.group(1)
            repo = m.group(2)

    def _extract_pr_url_from_payload(payload: dict | None) -> str:
        if not isinstance(payload, dict):
            return ""
        detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload
        if not isinstance(detail, dict):
            return ""
        links = detail.get("links") or {}
        self_links = links.get("self") or []
        if isinstance(self_links, list) and self_links and isinstance(self_links[0], dict):
            href = str(self_links[0].get("href") or "").strip()
            if href:
                return href
        return str(
            detail.get("htmlUrl")
            or detail.get("html_url")
            or detail.get("pr_url")
            or detail.get("prUrl")
            or detail.get("url")
            or ""
        ).strip()

    def _extract_pr_url_from_text(text: str) -> str:
        if not text:
            return ""
        try:
            return _extract_pr_url_from_payload(json.loads(text))
        except Exception:
            pass
        url_match = re.search(r"https?://[^\s)>'\"]+", text)
        return url_match.group(0) if url_match else ""

    try:
        scm_service_url = _resolve_agent_service_url("scm.pr.create")
        message = {
            "messageId": f"android-{task_id}-pr-{int(time.time())}",
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
        status_text = extract_text((downstream.get("status") or {}).get("message") or {})
        status_url = _extract_pr_url_from_text(status_text)
        if status_url:
            return status_url
        for art in (downstream.get("artifacts") or []):
            metadata_url = _extract_pr_url_from_payload(art.get("metadata") or {})
            if metadata_url:
                return metadata_url
            text_url = _extract_pr_url_from_text(artifact_text(art))
            if text_url:
                return text_url
        return ""
    except Exception as err:
        print(f"[{AGENT_ID}] Could not create PR: {err}")
        return ""


def _sanitize_base_branch(branch: str) -> str:
    if not branch:
        return "main"
    if re.match(r"^[A-Z][A-Z0-9]+-\d+", branch):
        return "main"
    if re.search(r"[\s~^:?*\[\\]", branch):
        return "main"
    return branch


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


def _detect_local_default_branch(repo_dir: str) -> str:
    ok, output = _run_local_git(
        repo_dir,
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        check=False,
    )
    if ok and output and "/" in output:
        return output.rsplit("/", 1)[-1].strip() or "main"
    return "main"


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


def _commit_local_changes(repo_dir: str, branch_name: str, files: list[dict], commit_message: str, log_fn) -> str:
    _run_local_git(repo_dir, ["config", "user.email", "android-agent@local"], check=False)
    _run_local_git(repo_dir, ["config", "user.name", "Android Agent"], check=False)

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


def _write_files_to_directory(base_dir: str, files: list[dict]) -> list[str]:
    if not base_dir:
        return []
    os.makedirs(base_dir, exist_ok=True)
    written: list[str] = []
    for file_info in files:
        rel_path = file_info.get("path", "output.txt").lstrip("/")
        full_path = os.path.join(base_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        content = file_info.get("content", "")
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        written.append(full_path)
    return written


def _ensure_gradle_wrapper_executable(build_dir: str) -> str:
    wrapper = os.path.join(build_dir, "gradlew")
    if os.path.isfile(wrapper):
        current_mode = os.stat(wrapper).st_mode
        os.chmod(wrapper, current_mode | 0o111)
        return "./gradlew"
    return "gradle"


def _resolve_android_sdk_dir() -> str:
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _android_gradle_env(build_dir: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    sdk_dir = _resolve_android_sdk_dir()
    if sdk_dir:
        env.setdefault("ANDROID_HOME", sdk_dir)
        env.setdefault("ANDROID_SDK_ROOT", sdk_dir)
    if build_dir:
        env.setdefault("GRADLE_USER_HOME", os.path.join(build_dir, ".gradle-agent"))
    env["CI"] = "true"
    return env


def _prepare_gradle_user_home_properties(build_dir: str, log_fn) -> None:
    """Write GRADLE_USER_HOME/gradle.properties with memory-constrained JVM args.

    Gradle reads JVM daemon args from gradle.properties in GRADLE_USER_HOME first,
    which overrides the project-level gradle.properties.  This is the only reliable
    way to cap the daemon heap from outside the project tree, preventing OOM crashes
    in memory-constrained containers (especially emulated amd64 on Apple Silicon).
    """
    env = _android_gradle_env(build_dir)
    gradle_home = env.get("GRADLE_USER_HOME", "")
    if not gradle_home:
        return
    jvm_args = os.environ.get("ANDROID_GRADLE_JVM_ARGS", "-Xmx1024m -Dfile.encoding=UTF-8").strip()
    if not jvm_args:
        return
    try:
        os.makedirs(gradle_home, exist_ok=True)
        props_path = os.path.join(gradle_home, "gradle.properties")
        with open(props_path, "w", encoding="utf-8") as fh:
            fh.write(f"# Written by Android Agent — overrides project gradle.properties JVM heap\n")
            fh.write(f"org.gradle.jvmargs={jvm_args}\n")
            fh.write("org.gradle.daemon=false\n")
        log_fn(f"Wrote GRADLE_USER_HOME/gradle.properties: org.gradle.jvmargs={jvm_args}")
    except OSError as exc:
        log_fn(f"Could not write GRADLE_USER_HOME/gradle.properties: {exc}")


def _android_gradle_base_args() -> list[str]:
    jvm_args = os.environ.get("ANDROID_GRADLE_JVM_ARGS", "-Xmx640m -Dfile.encoding=UTF-8").strip()
    max_workers = os.environ.get("ANDROID_GRADLE_MAX_WORKERS", "1").strip()
    args = [
        f"--max-workers={max_workers}" if max_workers else "",
        "--no-daemon",
        "--console=plain",
        "-Pkotlin.compiler.execution.strategy=in-process",
        "-Dkotlin.daemon.enabled=false",
        "-Dorg.gradle.vfs.watch=false",
    ]
    args = [arg for arg in args if arg]
    if jvm_args:
        args.append(f"-Dorg.gradle.jvmargs={jvm_args}")
    return args


def _android_gradle_command(gradle_cmd: str, *task_args: str) -> list[str]:
    return [gradle_cmd, *_android_gradle_base_args(), *task_args]


def _read_android_source_files(build_dir: str, max_files: int = 30) -> list[dict]:
    files: list[dict] = []
    skip_dirs = {
        ".git", ".gradle", ".gradle-agent", "build", ".idea", "node_modules",
        "__pycache__", ".pytest_cache", "out",
    }
    source_exts = {
        ".kt", ".java", ".xml", ".gradle", ".kts", ".properties", ".md", ".txt", ".json",
    }
    for root, dirs, fnames in os.walk(build_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in sorted(fnames):
            if len(files) >= max_files:
                return files
            _, ext = os.path.splitext(fname)
            if ext.lower() not in source_exts:
                continue
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, build_dir)
            try:
                with open(full_path, encoding="utf-8", errors="replace") as fh:
                    files.append({"path": rel_path, "content": fh.read(6000)})
            except Exception:
                continue
    return files


def _normalize_fix_entries(fixes: object) -> list[dict]:
    normalized: list[dict] = []
    if not isinstance(fixes, list):
        return normalized
    for fix in fixes:
        if not isinstance(fix, dict):
            continue
        rel_path = str(fix.get("path") or "").strip().lstrip("/")
        content = fix.get("content")
        if not rel_path or content is None:
            continue
        normalized.append({"path": rel_path, "content": str(content)})
    return normalized


def _apply_llm_fixes(build_dir: str, fixes: list[dict], log_fn) -> list[str]:
    applied_paths: list[str] = []
    for fix in fixes:
        rel_path = str(fix.get("path") or "").strip().lstrip("/")
        content = fix.get("content")
        if not rel_path or content is None:
            continue
        full_path = os.path.join(build_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        try:
            with open(full_path, "w", encoding="utf-8") as fh:
                fh.write(str(content))
            applied_paths.append(rel_path)
            log_fn(f"Applied build-fix patch to {rel_path}")
        except OSError as exc:
            log_fn(f"Could not apply build-fix patch to {rel_path}: {exc}")
    return applied_paths


def _sync_generated_files_from_repo(base_dir: str, generated_files: list[dict], candidate_paths: list[str]) -> None:
    existing_index = {
        str(file_info.get("path") or "").lstrip("/"): idx
        for idx, file_info in enumerate(generated_files)
        if isinstance(file_info, dict)
    }
    for rel_path in dict.fromkeys(path.lstrip("/") for path in candidate_paths if path):
        full_path = os.path.join(base_dir, rel_path)
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        if rel_path in existing_index:
            generated_files[existing_index[rel_path]]["content"] = content
        else:
            generated_files.append({"path": rel_path, "content": content})


def _build_and_test_with_recovery(
    build_dir: str,
    task_instruction: str,
    review_issues: list[str],
    log_fn,
) -> tuple[bool, str, list[dict], list[str]]:
    attempts: list[dict] = []
    final_output = ""
    repaired_paths: list[str] = []

    for repair_attempt in range(1, MAX_BUILD_RETRIES + 1):
        log_fn(f"Build/test attempt {repair_attempt}/{MAX_BUILD_RETRIES}")
        success, output, step_attempts = _build_and_test_android(build_dir, log_fn)
        final_output = output
        for step_attempt in step_attempts:
            attempts.append(
                {
                    "attempt": repair_attempt,
                    "label": step_attempt.get("label", "android:build"),
                    "success": bool(step_attempt.get("success")),
                    "output": str(step_attempt.get("output") or "")[:4000],
                }
            )
        if success:
            return True, final_output, attempts, repaired_paths

        log_fn(f"Build/test failed (attempt {repair_attempt}): {final_output[:200]}")
        if repair_attempt >= MAX_BUILD_RETRIES:
            break

        fix_prompt = prompts.BUILD_FIX_TEMPLATE.format(
            failure_output=final_output[:4000],
            source_files_json=json.dumps(_read_android_source_files(build_dir), ensure_ascii=False, indent=2)[:12000],
            task_instruction=task_instruction[:2000],
            review_feedback=("\n".join(review_issues) or "(none)"),
        )
        fix_response = _run_agentic(
            fix_prompt,
            f"build-fix-attempt-{repair_attempt}",
            system_prompt=prompts.BUILD_FIX_SYSTEM,
            max_tokens=8192,
        )
        fix_data = _parse_json_from_llm(fix_response)
        diagnosis = str(fix_data.get("diagnosis") or "unknown")
        fixes = _normalize_fix_entries(fix_data.get("fixes"))
        log_fn(f"LLM diagnosis: {diagnosis} — {len(fixes)} fix(es) to apply")
        if not fixes:
            log_fn("LLM produced no build-fix patches — stopping retry loop")
            break

        applied = _apply_llm_fixes(build_dir, fixes, log_fn)
        repaired_paths.extend(applied)
        if not applied:
            log_fn("No build-fix patches were applied — stopping retry loop")
            break

    return False, final_output, attempts, repaired_paths


def _prepare_android_local_properties(build_dir: str, log_fn) -> str:
    sdk_dir = _resolve_android_sdk_dir()
    if not sdk_dir:
        log_fn("ANDROID_HOME/ANDROID_SDK_ROOT is not configured; Gradle may not find the Android SDK")
        return ""

    local_properties_path = os.path.join(build_dir, "local.properties")
    desired_line = f"sdk.dir={sdk_dir}"
    existing = ""
    if os.path.isfile(local_properties_path):
        try:
            with open(local_properties_path, encoding="utf-8") as fh:
                existing = fh.read().strip()
        except OSError:
            existing = ""
        if desired_line in existing.splitlines():
            return sdk_dir

    with open(local_properties_path, "w", encoding="utf-8") as fh:
        fh.write(f"{desired_line}\n")
    log_fn(f"Prepared Android SDK local.properties using {sdk_dir}")
    return sdk_dir


def _detect_android_build_steps(build_dir: str) -> list[dict]:
    gradle_cmd = _ensure_gradle_wrapper_executable(build_dir)
    tasks_result = subprocess.run(
        _android_gradle_command(gradle_cmd, "tasks", "--all"),
        cwd=build_dir,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("ANDROID_GRADLE_DISCOVERY_TIMEOUT_SECONDS", "480")),
        check=False,
        env=_android_gradle_env(build_dir),
    )
    tasks_text = f"{tasks_result.stdout}\n{tasks_result.stderr}".lower()
    steps: list[dict] = []

    if "testdebugunittest" in tasks_text:
        steps.append({"cmd": _android_gradle_command(gradle_cmd, "testDebugUnitTest"), "label": "android:testDebugUnitTest"})
    elif re.search(r"(?m)^test\b", tasks_text):
        steps.append({"cmd": _android_gradle_command(gradle_cmd, "test"), "label": "android:test"})
    elif re.search(r"(?m)^check\b", tasks_text):
        steps.append({"cmd": _android_gradle_command(gradle_cmd, "check"), "label": "android:check"})

    if "assembledebug" in tasks_text:
        steps.append({"cmd": _android_gradle_command(gradle_cmd, "assembleDebug"), "label": "android:assembleDebug"})
    elif re.search(r"(?m)^assemble\b", tasks_text):
        steps.append({"cmd": _android_gradle_command(gradle_cmd, "assemble"), "label": "android:assemble"})
    elif re.search(r"(?m)^build\b", tasks_text):
        steps.append({"cmd": _android_gradle_command(gradle_cmd, "build"), "label": "android:build"})

    if not steps and os.path.isfile(os.path.join(build_dir, "gradlew")):
        steps = [
            {"cmd": _android_gradle_command(gradle_cmd, "testDebugUnitTest"), "label": "android:testDebugUnitTest"},
            {"cmd": _android_gradle_command(gradle_cmd, "assembleDebug"), "label": "android:assembleDebug"},
        ]
    return steps


def _clear_stale_gradle_locks(build_dir: str, log_fn) -> None:
    """Remove stale Gradle journal lock files left by previously killed containers.

    Gradle creates lock files under GRADLE_USER_HOME/caches/journal-1/.  If the
    container is killed mid-build these locks are never released, causing the next
    build to wait for a timeout and then fail.  It is safe to delete them before
    starting a fresh build because no live Gradle process is running at this point.
    """
    gradle_home = _android_gradle_env(build_dir).get(
        "GRADLE_USER_HOME",
        os.path.join(build_dir, ".gradle-agent"),
    )
    lock_patterns = [
        os.path.join(gradle_home, "caches", "journal-1", "journal-1.lock"),
    ]
    for lock_path in lock_patterns:
        if os.path.isfile(lock_path):
            try:
                os.remove(lock_path)
                log_fn(f"Removed stale Gradle lock: {lock_path}")
            except OSError as exc:
                log_fn(f"Could not remove stale Gradle lock {lock_path}: {exc}")


def _build_and_test_android(build_dir: str, log_fn) -> tuple[bool, str, list[dict]]:
    if not os.path.isdir(build_dir):
        return False, "Build directory is missing.", [{"attempt": 1, "success": False, "output": "Build directory is missing."}]

    if not os.path.isfile(os.path.join(build_dir, "gradlew")) and not os.path.isfile(os.path.join(build_dir, "build.gradle")) and not os.path.isfile(os.path.join(build_dir, "build.gradle.kts")):
        return False, "No Gradle wrapper or build file found.", [{"attempt": 1, "success": False, "output": "No Gradle wrapper or build file found."}]

    _clear_stale_gradle_locks(build_dir, log_fn)
    _prepare_gradle_user_home_properties(build_dir, log_fn)
    _prepare_android_local_properties(build_dir, log_fn)
    try:
        steps = _detect_android_build_steps(build_dir)
    except Exception as exc:
        gradle_cmd = _ensure_gradle_wrapper_executable(build_dir)
        if os.path.isfile(os.path.join(build_dir, "gradlew")):
            log_fn(f"Gradle task discovery failed ({exc}); falling back to default Android build/test steps")
            steps = [
                {"cmd": _android_gradle_command(gradle_cmd, "testDebugUnitTest"), "label": "android:testDebugUnitTest"},
                {"cmd": _android_gradle_command(gradle_cmd, "assembleDebug"), "label": "android:assembleDebug"},
            ]
        else:
            return False, f"Could not inspect Gradle tasks: {exc}", [{"attempt": 1, "success": False, "output": f"Could not inspect Gradle tasks: {exc}"}]

    if not steps:
        return False, "No usable Gradle build/test tasks found.", [{"attempt": 1, "success": False, "output": "No usable Gradle build/test tasks found."}]

    attempts: list[dict] = []
    outputs: list[str] = []
    for index, step in enumerate(steps, start=1):
        log_fn(f"Build/test step {index}/{len(steps)}: {step['label']}")
        result = subprocess.run(
            step["cmd"],
            cwd=build_dir,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("ANDROID_GRADLE_STEP_TIMEOUT_SECONDS", "1800")),
            check=False,
            env=_android_gradle_env(build_dir),
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        outputs.append(f"$ {' '.join(step['cmd'])}\n{output}".strip())
        attempts.append(
            {
                "attempt": index,
                "label": step["label"],
                "success": result.returncode == 0,
                "output": output[:4000],
            }
        )
        if result.returncode != 0:
            return False, "\n\n".join(outputs), attempts
    return True, "\n\n".join(outputs), attempts


# ---------------------------------------------------------------------------
# Repo context helpers
# ---------------------------------------------------------------------------

def _read_repo_snapshot(clone_path: str, max_files: int = 30, max_chars: int = 8000) -> str:
    if not clone_path or not os.path.isdir(clone_path):
        return ""
    snapshot_parts: list[str] = []
    chars_used = 0
    files_read = 0

    # Android-specific priority files
    priority_patterns = [
        "build.gradle", "build.gradle.kts",
        "settings.gradle", "settings.gradle.kts",
        "gradle.properties", "README.md",
        "app/build.gradle", "app/build.gradle.kts",
        "app/src/main/AndroidManifest.xml",
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

    for pattern in priority_patterns:
        candidate = os.path.join(clone_path, pattern)
        if os.path.isfile(candidate) and chars_used < max_chars and files_read < max_files:
            content = _read_file_safe(candidate)
            if content:
                rel = os.path.relpath(candidate, clone_path)
                snapshot_parts.append(f"=== {rel} ===\n{content}")
                chars_used += len(content)
                files_read += 1

    skip_dirs = {
        ".git", "node_modules", "__pycache__", "build", ".gradle",
        ".idea", "out", ".cxx", ".externalNativeBuild",
    }
    source_exts = {
        ".kt", ".java", ".xml", ".gradle", ".kts", ".properties",
        ".json", ".md", ".pro", ".cfg",
    }
    for root, dirs, files in os.walk(clone_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if chars_used >= max_chars or files_read >= max_files:
            break
        for fname in sorted(files):
            if chars_used >= max_chars or files_read >= max_files:
                break
            ext = os.path.splitext(fname)[1].lower()
            if ext not in source_exts:
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, clone_path)
            if any(f"=== {rel} ===" in p for p in snapshot_parts):
                continue
            content = _read_file_safe(full)
            if content:
                snapshot_parts.append(f"=== {rel} ===\n{content}")
                chars_used += len(content)
                files_read += 1

    return "\n\n".join(snapshot_parts)


def _get_repo_tree(clone_path: str, max_depth: int = 5) -> str:
    """Generate a directory tree listing for a cloned repo."""
    if not clone_path or not os.path.isdir(clone_path):
        return ""
    skip_dirs = {
        ".git", "node_modules", "__pycache__", "build", ".gradle",
        ".idea", "out", ".cxx",
    }
    lines: list[str] = []
    for root, dirs, files in os.walk(clone_path):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        depth = root.replace(clone_path, "").count(os.sep)
        if depth >= max_depth:
            dirs.clear()
            continue
        indent = "  " * depth
        rel = os.path.relpath(root, clone_path)
        if rel == ".":
            lines.append("/")
        else:
            lines.append(f"{indent}{os.path.basename(root)}/")
        sub_indent = "  " * (depth + 1)
        for f in sorted(files):
            lines.append(f"{sub_indent}{f}")
    return "\n".join(lines[:500])


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _parse_json_from_llm(raw: str) -> dict:
    clean = (raw or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z]*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean.strip())
    try:
        result = json.loads(clean)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return {}


def _run_agentic(prompt: str, label: str, *, system_prompt: str = "", max_tokens: int = 4096) -> str:
    runtime = get_runtime()
    result = runtime.run(
        prompt=prompt,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
    )
    return result.get("raw_response") or result.get("summary") or ""


def _discover_files(ticket_context: dict, repo_tree: str, readme_content: str) -> dict:
    """Use LLM to discover which files to read for implementation context."""
    prompt = prompts.FILE_DISCOVERY_PROMPT.format(
        ticket_key=ticket_context.get("ticket_key", "unknown"),
        ticket_title=ticket_context.get("title", ""),
        ticket_description=ticket_context.get("description", ""),
        repo_project=ticket_context.get("repo_project", ""),
        repo_name=ticket_context.get("repo_name", ""),
        repo_tree=repo_tree or "(not available)",
        readme_chars=3000,
        readme_content=(readme_content or "(no README found)")[:3000],
    )
    raw = _run_agentic(prompt, "file-discovery")
    result = _parse_json_from_llm(raw)
    if isinstance(result.get("files_to_read"), list):
        return result
    return {"files_to_read": [], "analysis": raw[:500]}


def _generate_implementation(ticket_context: dict, repo_tree: str, file_contents: dict) -> dict:
    """Use LLM to generate implementation files."""
    if file_contents:
        files_block = "\n\n".join(
            f"### {path}\n```\n{content[:8000]}\n```"
            for path, content in file_contents.items()
            if content
        )
    else:
        files_block = "(no files read — working from directory structure and ticket description only)"

    prompt = prompts.IMPLEMENTATION_GENERATION_PROMPT.format(
        ticket_key=ticket_context.get("ticket_key", "unknown"),
        ticket_title=ticket_context.get("title", ""),
        ticket_description=ticket_context.get("description", ""),
        repo_project=ticket_context.get("repo_project", ""),
        repo_name=ticket_context.get("repo_name", ""),
        repo_url=ticket_context.get("repo_url", ""),
        file_contents=files_block,
        repo_tree_summary=(repo_tree or "")[:2000],
        additional_context=ticket_context.get("additional_context", "(none)"),
    )
    raw = _run_agentic(prompt, "implementation", max_tokens=8192)
    result = _parse_json_from_llm(raw)
    if isinstance(result.get("files"), list):
        normalized = []
        for f in result["files"]:
            if isinstance(f, dict) and f.get("path") and f.get("content") is not None:
                normalized.append({"path": f["path"], "content": f["content"]})
        result["files"] = normalized
        return result
    raise RuntimeError(f"LLM returned unparseable implementation. Raw (first 500): {raw[:500]}")


def _save_pr_evidence(workspace_path: str, **details) -> None:
    payload = _read_workspace_json(workspace_path, f"{AGENT_ID}/pr-evidence.json")
    payload.update({k: v for k, v in details.items() if v is not None})
    payload.setdefault("agentId", AGENT_ID)
    payload.setdefault("ts", local_iso_timestamp())
    _save_workspace_file(
        workspace_path,
        f"{AGENT_ID}/pr-evidence.json",
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def _run_workflow(task_id: str, message: dict):  # noqa: C901
    """
    Full Android Agent workflow running in a background thread.

    Phases:
      ANALYZING → GATHERING → PLANNING → IMPLEMENTING → PUSHING → COMPLETED
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
    acceptance_criteria: list = metadata.get("acceptanceCriteria") or []
    is_revision: bool = metadata.get("isRevision", False)
    review_issues: list = metadata.get("reviewIssues") or []
    tech_stack_constraints: dict = metadata.get("techStackConstraints") or {}
    design_context_meta: dict = metadata.get("designContext") or {}
    metadata_repo_url: str = metadata.get("targetRepoUrl", "")
    exit_rule = PerTaskExitHandler.parse(metadata)
    permissions: dict | None = metadata.get("permissions") if isinstance(metadata.get("permissions"), dict) else None

    task_instruction = extract_text(message) or ""
    final_artifacts: list = []
    repo_url = metadata_repo_url or ""
    clone_path = ""
    branch_name = ""
    pr_url = ""
    local_commit_sha = ""
    build_ok: bool | None = None
    build_output = ""
    agent_workspace = os.path.join(workspace, AGENT_ID) if workspace else ""
    runtime_config = {
        "runtime": summarize_runtime_configuration(),
        "techStackConstraints": tech_stack_constraints,
    }

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
                    "localCommit": local_commit_sha,
                    "prUrl": pr_url,
                    "buildPassed": build_ok,
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

        # Handle revision mode
        if is_revision and review_issues:
            issues_text = "\n".join(f"- {issue}" for issue in review_issues)
            task_instruction = (
                f"{task_instruction}\n\n"
                f"REVISION REQUEST — please fix the following issues:\n{issues_text}"
            )

        # Restore clone/branch state from prior task in the same workspace
        if is_revision and workspace:
            _ci = _read_workspace_json(workspace, f"{AGENT_ID}/clone-info.json")
            _bi = _read_workspace_json(workspace, f"{AGENT_ID}/branch-info.json")
            if _ci and _ci.get("clonePath") and os.path.isdir(_ci["clonePath"]):
                clone_path = _ci["clonePath"]
                repo_url = repo_url or _ci.get("repoUrl", "")
                log(f"Revision: reusing existing clone at {clone_path}")
            if _bi and _bi.get("branch"):
                branch_name = _bi["branch"]
                pr_url = _bi.get("prUrl", "")
                log(f"Revision: restored branch={branch_name} pr={pr_url}")

        # ── Phase 1: Gather context ─────────────────────────────────────────
        task_store.update_state(task_id, "GATHERING_INFO", "Gathering Jira and repo context…")
        log("Gathering context")

        ticket_key, jira_content = _resolve_jira_context_from_metadata(task_instruction, metadata)
        if jira_content:
            log(f"Using Jira context from Team Lead metadata for {ticket_key or 'provided ticket'}")
            task_instruction = (
                f"{task_instruction}\n\nJira ticket context ({ticket_key or 'provided ticket'}):\n{jira_content[:3000]}"
            )
        elif ticket_key and workspace:
            log(f"Fetching Jira context for {ticket_key}")
            jira_content = _fetch_jira_context(task_id, ticket_key, workspace, compass_task_id, permissions=permissions)
            if jira_content:
                log(f"Jira ticket {ticket_key} fetched ({len(jira_content)} chars)")
                task_instruction = (
                    f"{task_instruction}\n\nJira ticket context ({ticket_key}):\n{jira_content[:3000]}"
                )

            # Mark ticket In Progress
            if not is_revision:
                log(f"Updating Jira ticket {ticket_key}: In Progress")
                _jira_transition(ticket_key, "In Progress", task_id, workspace, compass_task_id, permissions=permissions)
                _jira_assign_self(ticket_key, task_id, workspace, compass_task_id, permissions=permissions)
                _jira_add_comment(
                    ticket_key,
                    f"[Android Agent] ({AGENT_ID}) has picked up this ticket and started development.\n"
                    f"Internal task ID: {workflow_task_id}",
                    task_id,
                    workspace,
                    compass_task_id,
                    permissions=permissions,
                )

        # Extract repo URL from instruction or metadata
        if not repo_url:
            url_match = (
                re.search(r"https?://[^\s]+\.git", task_instruction)
                or re.search(r"https?://github\.com/[^\s]+", task_instruction)
                or re.search(r"https?://[^\s]*/projects/[^\s]+/repos/[^\s]+", task_instruction)
                or re.search(r"https?://[^\s]*/scm/[^\s]+", task_instruction)
            )
            if url_match:
                repo_url = url_match.group().rstrip("/.,;)")

        # Clone repo
        repo_snapshot = ""
        repo_tree = ""
        if repo_url and workspace and not clone_path:
            log(f"Cloning repository: {repo_url}")
            try:
                clone_path = _clone_repo(task_id, repo_url, workspace, compass_task_id, permissions=permissions)
            except Exception as err:
                _save_workspace_file(
                    workspace,
                    f"{AGENT_ID}/clone-info.json",
                    json.dumps(
                        {"taskId": workflow_task_id, "agentId": AGENT_ID,
                         "repoUrl": repo_url, "status": "failed", "error": str(err)},
                        ensure_ascii=False, indent=2,
                    ),
                )
                raise
            if clone_path:
                log(f"Repository cloned to {clone_path}")
                _save_workspace_file(
                    workspace,
                    f"{AGENT_ID}/clone-info.json",
                    json.dumps(
                        {"taskId": workflow_task_id, "agentId": AGENT_ID,
                         "repoUrl": repo_url, "clonePath": clone_path, "status": "completed"},
                        ensure_ascii=False, indent=2,
                    ),
                )
        elif clone_path:
            log(f"Using existing clone at {clone_path}")

        if clone_path:
            repo_snapshot = _read_repo_snapshot(clone_path)
            repo_tree = _get_repo_tree(clone_path)

        # ── Phase 2: Discover files to read ──────────────────────────────────
        task_store.update_state(task_id, "ANALYZING", "Analyzing repo and discovering relevant files…")
        log("Discovering files to read")

        # Parse Jira description
        jira_data = {}
        try:
            jira_data = json.loads(jira_content) if jira_content else {}
        except Exception:
            pass
        fields = jira_data.get("fields") or {}
        title = str(fields.get("summary") or "").strip()
        raw_desc = fields.get("description")
        if isinstance(raw_desc, str):
            description = raw_desc.strip()
        elif isinstance(raw_desc, dict):
            # ADF format — extract plain text
            description = _extract_adf_text(raw_desc)
        else:
            description = task_instruction

        # Extract repo project/name from URL
        repo_project, repo_name = "", ""
        m = re.search(r"/projects/([^/]+)/repos/([^/\s?#]+)", repo_url or "")
        if m:
            repo_project, repo_name = m.group(1), m.group(2)
        else:
            m = re.search(r"github\.com/([^/]+)/([^/\s?#]+)", repo_url or "")
            if m:
                repo_project, repo_name = m.group(1), m.group(2).rstrip(".git")

        # Build design context
        additional_context_parts = []
        if design_context_meta and design_context_meta.get("content"):
            additional_context_parts.append(design_context_meta["content"])
        # Pass review feedback into the implementation prompt so the LLM knows
        # exactly which files to create/fix during a revision.
        if is_revision and review_issues:
            revision_block = (
                "REVISION REQUIRED — Code review identified these issues that must be fixed:\n"
                + "\n".join(f"  - {issue}" for issue in review_issues)
                + "\n\nEnsure ALL issues above are resolved and ALL required files are created."
            )
            additional_context_parts.append(revision_block)

        ticket_context = {
            "ticket_key": ticket_key or "unknown",
            "title": title or f"Implement {ticket_key}",
            "description": description or task_instruction,
            "repo_url": repo_url,
            "repo_project": repo_project,
            "repo_name": repo_name or "repository",
            "additional_context": "\n\n".join(additional_context_parts),
        }

        # Read README
        readme_content = ""
        if clone_path:
            for readme_name in ("README.md", "readme.md", "Readme.md"):
                readme_path = os.path.join(clone_path, readme_name)
                if os.path.isfile(readme_path):
                    try:
                        with open(readme_path, encoding="utf-8") as fh:
                            readme_content = fh.read(3000)
                    except Exception:
                        pass
                    break

        discovery = _discover_files(ticket_context, repo_tree, readme_content)
        files_to_read = discovery.get("files_to_read") or []
        log(f"LLM file discovery: {len(files_to_read)} files identified")

        # Read discovered files
        file_contents = {}
        for file_path in files_to_read[:15]:
            full_path = os.path.join(clone_path, file_path)
            if os.path.isfile(full_path):
                try:
                    with open(full_path, encoding="utf-8", errors="replace") as fh:
                        file_contents[file_path] = fh.read(8000)
                except Exception:
                    pass
        if file_contents:
            log(f"Read {len(file_contents)} files: " + ", ".join(list(file_contents.keys())[:5]))

        # ── Phase 3: Generate implementation ─────────────────────────────────
        task_store.update_state(task_id, "IMPLEMENTING", "Generating Android implementation…")
        log("Generating implementation")

        implementation = _generate_implementation(ticket_context, repo_tree, file_contents)
        impl_goal = implementation.get("goal") or f"Implement {ticket_key}"
        impl_files = implementation.get("files") or []
        impl_files_to_delete = implementation.get("files_to_delete") or []
        impl_pr_desc = implementation.get("pr_description") or f"## {ticket_key}\n\n{impl_goal}"

        if not impl_files and not impl_files_to_delete:
            raise RuntimeError(f"No implementation files generated for {ticket_key}")

        log(f"Generated {len(impl_files)} file(s), {len(impl_files_to_delete)} deletion(s)")

        # ── Phase 4: Prepare local branch, validate, push, and create PR ────
        task_store.update_state(task_id, "PUSHING", "Preparing local branch, validating, and creating PR…")

        # Determine branch name
        if not branch_name:
            branch_seed = re.sub(r"[^A-Za-z0-9._-]+", "-", ticket_key or f"android-task-{workflow_task_id}").strip("-._")
            branch_name = f"feature/{branch_seed or workflow_task_id}"

        # Determine base branch from repo or use main
        base_branch = _detect_local_default_branch(clone_path) if clone_path else "main"
        if base_branch == "main" and repo_snapshot:
            for line in repo_snapshot.split("\n"):
                if "defaultBranch" in line:
                    parts = line.split(":")
                    if len(parts) > 1:
                        candidate = parts[1].strip().strip('"').strip(",")
                        if candidate:
                            base_branch = candidate
                            break

        # Deduplicate: append _<n> if the branch already exists remotely.
        # Skip this in revision mode — the branch was already created; reuse it.
        if not is_revision:
            remote_branches = _list_remote_branches(task_id, repo_url, workspace, compass_task_id, permissions=permissions)
            branch_name = _unique_branch_name(branch_name, remote_branches)

        log(f"Branch: {branch_name} (base: {base_branch})")

        local_branch_prepared = False
        if clone_path:
            _checkout_local_branch(clone_path, branch_name, base_branch, log)
            local_branch_prepared = True
            written_clone_paths = _write_files_to_directory(clone_path, impl_files)
            log(f"Wrote {len(written_clone_paths)} file(s) into cloned repository")

        # Create branch
        _create_branch(task_id, repo_url, branch_name, base_branch, workspace, compass_task_id, permissions=permissions)

        _save_workspace_file(
            workspace,
            f"{AGENT_ID}/branch-info.json",
            json.dumps(
                {
                    "taskId": workflow_task_id,
                    "agentTaskId": task_id,
                    "agentId": AGENT_ID,
                    "branch": branch_name,
                    "baseBranch": base_branch,
                    "repoUrl": repo_url,
                    "clonePath": clone_path,
                    "localBranchPrepared": local_branch_prepared,
                    "localCommit": local_commit_sha,
                    "prUrl": pr_url,
                    "buildPassed": build_ok,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

        task_store.update_state(task_id, "BUILDING", "Running Android build and tests…")
        repaired_paths: list[str] = []
        if clone_path:
            build_ok, build_output, build_attempts, repaired_paths = _build_and_test_with_recovery(
                clone_path,
                task_instruction,
                review_issues,
                log,
            )
        else:
            build_ok, build_output, build_attempts, repaired_paths = False, "No cloned repository available for build/test.", [
                {"attempt": 1, "label": "android:build", "success": False, "output": "No cloned repository available for build/test."}
            ], []
        if clone_path:
            _sync_generated_files_from_repo(
                clone_path,
                impl_files,
                [file_info.get("path", "") for file_info in impl_files] + repaired_paths,
            )
        _save_workspace_file(
            workspace,
            f"{AGENT_ID}/test-results.json",
            json.dumps(
                {
                    "taskId": workflow_task_id,
                    "agentTaskId": task_id,
                    "agentId": AGENT_ID,
                    "buildDir": clone_path,
                    "passed": build_ok,
                    "attempts": build_attempts,
                    "finalOutput": (build_output or "")[:4000],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        if build_ok:
            log("Build/tests passed")
        else:
            log(f"Build/tests failed or were incomplete: {(build_output or '')[:200]}")

        # Push files
        commit_msg = f"feat({ticket_key}): {impl_goal} [android-agent]"
        if clone_path and local_branch_prepared:
            local_commit_sha = _commit_local_changes(clone_path, branch_name, impl_files, commit_msg, log)
        push_ok = _push_files(
            task_id, repo_url, branch_name, impl_files,
            commit_msg, workspace, compass_task_id, base_branch,
            permissions=permissions,
        )
        if not push_ok:
            raise RuntimeError(f"Failed to push files to {branch_name}")

        log(f"Pushed {len(impl_files)} file(s) to {branch_name}")

        # Save branch info for potential revisions
        _save_workspace_file(
            workspace,
            f"{AGENT_ID}/branch-info.json",
            json.dumps(
                {
                    "taskId": workflow_task_id,
                    "agentTaskId": task_id,
                    "agentId": AGENT_ID,
                    "branch": branch_name,
                    "baseBranch": base_branch,
                    "repoUrl": repo_url,
                    "clonePath": clone_path,
                    "localBranchPrepared": local_branch_prepared,
                    "localCommit": local_commit_sha,
                    "prUrl": pr_url,
                    "buildPassed": build_ok,
                },
                ensure_ascii=False, indent=2,
            ),
        )

        # Create PR
        pr_title = f"[{ticket_key}] {impl_goal}" if ticket_key else impl_goal
        test_status = "✅ Build/tests passed" if build_ok else f"⚠️ Build/tests had issues\n{(build_output or '')[:800]}"
        if "## Testing" not in impl_pr_desc:
            impl_pr_desc = f"{impl_pr_desc}\n\n## Testing\n- {test_status}"
        pr_url = _create_pr(
            task_id, repo_url, branch_name, base_branch,
            pr_title, impl_pr_desc, workspace, compass_task_id,
            permissions=permissions,
        )
        log(f"PR created: {pr_url or '(URL not captured)'}")

        # Save PR evidence — use field names compatible with the review evidence
        # loader (url, generatedFiles) AND the legacy names (prUrl, filesChanged).
        changed_files = [f.get("path") for f in impl_files]
        _save_pr_evidence(
            workspace,
            taskId=workflow_task_id,
            agentTaskId=task_id,
            repoUrl=repo_url,
            branch=branch_name,
            baseBranch=base_branch,
            clonePath=clone_path,
            localCommit=local_commit_sha,
            buildPassed=build_ok,
            prUrl=pr_url,
            url=pr_url,          # alias expected by review evidence loader
            prTitle=pr_title,
            prBody=impl_pr_desc,
            filesChanged=changed_files,
            generatedFiles=changed_files,  # alias expected by review evidence loader
            filesDeleted=impl_files_to_delete,
        )

        # ── Phase 5: Update Jira and complete ────────────────────────────────
        task_store.update_state(task_id, "COMPLETING", "Finalizing…")

        jira_in_review = False
        if ticket_key:
            _jira_transition(ticket_key, "In Review", task_id, workspace, compass_task_id, permissions=permissions)
            jira_in_review = True
            _jira_add_comment(
                ticket_key,
                f"[Android Agent] Implementation complete — PR raised.\n"
                f"Branch: {branch_name}\nPR: {pr_url or '(pending)'}\n{test_status}",
                task_id,
                workspace,
                compass_task_id,
                permissions=permissions,
            )

        # Build summary
        file_list = ", ".join(f.get("path", "") for f in impl_files[:5])
        if len(impl_files) > 5:
            file_list += "…"
        summary = (
            f"Android implementation complete for {ticket_key or 'task'}.\n"
            f"Goal: {impl_goal}\n"
            f"Branch: {branch_name}\n"
            f"PR: {pr_url or '(pending)'}\n"
            f"Files changed ({len(impl_files)}): {file_list}"
        )
        if impl_files_to_delete:
            summary += f"\nFiles deleted ({len(impl_files_to_delete)}): {', '.join(impl_files_to_delete[:3])}"

        final_artifacts = [
            build_text_artifact(
                "implementation-summary",
                summary,
                metadata={
                    "agentId": AGENT_ID,
                    "capability": "android.task.execute",
                    "orchestratorTaskId": compass_task_id,
                    "taskId": task_id,
                    "prUrl": pr_url,
                    "url": pr_url,      # alias used by Compass evidence extraction
                    "branch": branch_name,
                    # jiraInReview is read by Compass to display "Completed / In Review"
                    # without having to scan the shared workspace filesystem.
                    "jiraInReview": jira_in_review,
                },
            ),
        ]

        task_store.update_state(task_id, "TASK_STATE_COMPLETED", summary)
        audit_log("TASK_COMPLETED", task_id=task_id, compass_task_id=compass_task_id,
                   pr_url=pr_url, files_changed=len(impl_files))

        log(f"Task completed: {pr_url or impl_goal}")

        _notify_callback(
            callback_url, task_id, "TASK_STATE_COMPLETED", summary, final_artifacts,
        )

    except Exception as exc:
        error_msg = f"Android agent failed: {exc}"
        print(f"[{AGENT_ID}] {error_msg}")
        audit_log("TASK_FAILED", task_id=task_id, error=str(exc))

        failure_artifacts = []
        if isinstance(exc, PermissionEscalationRequired):
            failure_artifacts = [build_permission_denied_artifact(exc.details, agent_id=AGENT_ID)]

        task_store.update_state(task_id, "TASK_STATE_FAILED", error_msg)
        task = task_store.get(task_id)
        if task:
            task.artifacts = failure_artifacts
        _notify_callback(callback_url, task_id, "TASK_STATE_FAILED", error_msg, failure_artifacts)

    finally:
        if exit_rule:
            _apply_task_exit_rule(task_id, exit_rule)


def _extract_adf_text(adf_body: dict | None) -> str:
    """Extract plain text from Atlassian Document Format (ADF)."""
    if not isinstance(adf_body, dict):
        return ""
    lines: list[str] = []
    for block in adf_body.get("content", []):
        if not isinstance(block, dict):
            continue
        parts: list[str] = []
        for inline in block.get("content", []):
            if isinstance(inline, dict) and inline.get("type") == "text":
                parts.append(str(inline.get("text", "")))
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class AndroidAgentHandler(BaseHTTPRequestHandler):
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
    print(f"[{AGENT_ID}] Android Agent starting on {HOST}:{PORT}")
    agent_directory.start()
    _SERVER = ThreadingHTTPServer((HOST, PORT), AndroidAgentHandler)
    reporter.start()
    _SERVER.serve_forever()


if __name__ == "__main__":
    main()
