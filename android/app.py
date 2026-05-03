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


def _append_workspace_event(workspace_path: str, relative_name: str, event: dict) -> None:
    payload = _read_workspace_json(workspace_path, relative_name)
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
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
    downstream = _a2a_send(agent_url, message)
    task_id_ds = downstream.get("id", "")
    state = downstream.get("status", {}).get("state", "")
    terminal = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED"}
    if state in terminal:
        return downstream
    if task_id_ds:
        result = _poll_task(agent_url, task_id_ds, timeout=SYNC_AGENT_TIMEOUT)
        if result:
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
    timeout: int = 30,
) -> dict:
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if workspace:
        headers["X-Shared-Workspace-Path"] = workspace
    if task_id:
        headers["X-Orchestrator-Task-Id"] = task_id
    headers["X-Agent-Id"] = AGENT_ID
    service_url = _resolve_agent_service_url(capability)
    request = Request(
        f"{service_url}{path}",
        data=(json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None),
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:300]}") from exc
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


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

def _fetch_jira_context(task_id: str, ticket_key: str, workspace: str, compass_task_id: str) -> str:
    try:
        result = _jira_request_json(
            "jira.ticket.fetch",
            "GET",
            f"/jira/tickets/{ticket_key}",
            workspace=workspace,
            task_id=task_id,
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


def _jira_transition(ticket_key: str, target_status: str, task_id: str, workspace: str, compass_task_id: str):
    try:
        _jira_request_json(
            "jira.ticket.transition",
            "POST",
            f"/jira/transitions/{ticket_key}",
            payload={"transition": target_status},
            workspace=workspace,
            task_id=task_id,
        )
        print(f"[{AGENT_ID}] Jira {ticket_key} transitioned to '{target_status}'")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "transition", "completed",
                            agent_task_id=task_id, targetStatus=target_status)
    except Exception as err:
        print(f"[{AGENT_ID}] Jira transition failed (non-critical): {err}")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "transition", "failed",
                            agent_task_id=task_id, targetStatus=target_status, error=str(err))


def _jira_assign_self(ticket_key: str, task_id: str, workspace: str, compass_task_id: str):
    try:
        response = _jira_request_json(
            "jira.user.myself",
            "GET",
            "/jira/myself",
            workspace=workspace,
            task_id=task_id,
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
        )
        print(f"[{AGENT_ID}] Jira {ticket_key} assigned to service account")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "assign", "completed",
                            agent_task_id=task_id, accountId=account_id)
    except Exception as err:
        print(f"[{AGENT_ID}] Jira assign failed (non-critical): {err}")
        _record_jira_action(workspace, compass_task_id or task_id, ticket_key, "assign", "failed",
                            agent_task_id=task_id, error=str(err))


def _jira_add_comment(ticket_key: str, comment: str, task_id: str, workspace: str, compass_task_id: str):
    try:
        _jira_request_json(
            "jira.comment.add",
            "POST",
            f"/jira/comments/{ticket_key}",
            payload={"text": comment},
            workspace=workspace,
            task_id=task_id,
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

def _clone_repo(task_id: str, repo_url: str, workspace: str, compass_task_id: str) -> str:
    result = _call_sync_agent(
        "scm.git.clone",
        f"Clone repository {repo_url} to {workspace}",
        task_id,
        workspace,
        compass_task_id,
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


def _list_remote_branches(task_id: str, repo_url: str, workspace: str, compass_task_id: str) -> set:
    """Return the set of remote branch names from the SCM agent (best-effort)."""
    try:
        result = _call_sync_agent(
            "scm.branch.list",
            f"List branches in {repo_url}",
            task_id,
            workspace,
            compass_task_id,
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
                    workspace: str, compass_task_id: str) -> bool:
    try:
        result = _call_sync_agent(
            "scm.branch.create",
            f"Create branch {branch_name} from {base_branch} in {repo_url}",
            task_id,
            workspace,
            compass_task_id,
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
) -> str:
    safe_base = _sanitize_base_branch(base_branch)
    owner, repo = "", ""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s?#]+)", repo_url or "")
    if m:
        owner = m.group(1)
        repo = m.group(2).rstrip(".git")
    if not owner:
        m = re.search(r"/projects/([^/]+)/repos/([^/\s?#]+)", repo_url or "")
        if m:
            owner = m.group(1)
            repo = m.group(2)
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

    task_instruction = extract_text(message) or ""
    final_artifacts: list = []
    repo_url = metadata_repo_url or ""
    clone_path = ""
    branch_name = ""
    pr_url = ""
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
                    "prUrl": pr_url,
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

        jira_content = ""
        ticket_key = ""
        # Team Lead passes the Jira ticket key explicitly in metadata to avoid
        # the regex accidentally matching technical terms like "UTF-8" or "ISO-8".
        ticket_key_from_meta = str(metadata.get("jiraTicketKey") or "").strip()
        if ticket_key_from_meta:
            ticket_key = ticket_key_from_meta
        else:
            # Fallback: scan instruction text.  Require at least 2 digits to
            # exclude version/encoding strings (UTF-8, ISO-8, HTTP-2, etc.).
            ticket_match = re.search(r"\b([A-Z][A-Z0-9]+-\d{2,})\b", task_instruction)
            if ticket_match:
                ticket_key = ticket_match.group(1)
        if ticket_key and workspace:
            log(f"Fetching Jira context for {ticket_key}")
            jira_content = _fetch_jira_context(task_id, ticket_key, workspace, compass_task_id)
            if jira_content:
                log(f"Jira ticket {ticket_key} fetched ({len(jira_content)} chars)")
                task_instruction = (
                    f"{task_instruction}\n\nJira ticket context ({ticket_key}):\n{jira_content[:3000]}"
                )

            # Mark ticket In Progress
            if not is_revision:
                log(f"Updating Jira ticket {ticket_key}: In Progress")
                _jira_transition(ticket_key, "In Progress", task_id, workspace, compass_task_id)
                _jira_assign_self(ticket_key, task_id, workspace, compass_task_id)
                _jira_add_comment(
                    ticket_key,
                    f"[Android Agent] ({AGENT_ID}) has picked up this ticket and started development.\n"
                    f"Internal task ID: {workflow_task_id}",
                    task_id,
                    workspace,
                    compass_task_id,
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
                clone_path = _clone_repo(task_id, repo_url, workspace, compass_task_id)
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

        # ── Phase 4: Push files and create PR ────────────────────────────────
        task_store.update_state(task_id, "PUSHING", "Pushing files and creating PR…")

        # Determine branch name
        if not branch_name:
            branch_name = f"agent/feature/{ticket_key or f'android-task-{task_id}'}"

        # Determine base branch from repo or use main
        base_branch = "main"
        if repo_snapshot:
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
            remote_branches = _list_remote_branches(task_id, repo_url, workspace, compass_task_id)
            branch_name = _unique_branch_name(branch_name, remote_branches)

        log(f"Branch: {branch_name} (base: {base_branch})")

        # Create branch
        _create_branch(task_id, repo_url, branch_name, base_branch, workspace, compass_task_id)

        # Push files
        commit_msg = f"feat({ticket_key}): {impl_goal} [android-agent]"
        push_ok = _push_files(
            task_id, repo_url, branch_name, impl_files,
            commit_msg, workspace, compass_task_id, base_branch,
        )
        if not push_ok:
            raise RuntimeError(f"Failed to push files to {branch_name}")

        log(f"Pushed {len(impl_files)} file(s) to {branch_name}")

        # Save branch info for potential revisions
        _save_workspace_file(
            workspace,
            f"{AGENT_ID}/branch-info.json",
            json.dumps(
                {"taskId": workflow_task_id, "agentId": AGENT_ID,
                 "branch": branch_name, "baseBranch": base_branch,
                 "repoUrl": repo_url},
                ensure_ascii=False, indent=2,
            ),
        )

        # Create PR
        pr_title = f"[{ticket_key}] {impl_goal}" if ticket_key else impl_goal
        pr_url = _create_pr(
            task_id, repo_url, branch_name, base_branch,
            pr_title, impl_pr_desc, workspace, compass_task_id,
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
            prUrl=pr_url,
            url=pr_url,          # alias expected by review evidence loader
            prTitle=pr_title,
            filesChanged=changed_files,
            generatedFiles=changed_files,  # alias expected by review evidence loader
            filesDeleted=impl_files_to_delete,
        )

        # ── Phase 5: Update Jira and complete ────────────────────────────────
        task_store.update_state(task_id, "COMPLETING", "Finalizing…")

        jira_in_review = False
        if ticket_key:
            _jira_transition(ticket_key, "In Review", task_id, workspace, compass_task_id)
            jira_in_review = True
            _jira_add_comment(
                ticket_key,
                f"[Android Agent] Implementation complete — PR raised.\n"
                f"Branch: {branch_name}\nPR: {pr_url or '(pending)'}",
                task_id,
                workspace,
                compass_task_id,
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

        task_store.update_state(task_id, "TASK_STATE_FAILED", error_msg)
        _notify_callback(callback_url, task_id, "TASK_STATE_FAILED", error_msg)

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
