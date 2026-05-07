"""Long-running Jira agent — ticket fetch, transitions, and comment CRUD.

Supports two back-ends selected via JIRA_BACKEND:
  rest (default) — Jira REST API v3
  mcp            — Atlassian Rovo MCP server (https://mcp.atlassian.com/v1/mcp)
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from common.devlog import debug_log, preview_data, record_workspace_stage
from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.message_utils import extract_text
from common.tools.control_tools import configure_control_tools
from common.prompt_builder import build_system_prompt_from_manifest
from common.runtime.adapter import get_runtime, require_agentic_runtime, summarize_runtime_configuration
from common.task_permissions import (
    PermissionDeniedError,
    audit_permission_check,
    build_permission_denied_artifact,
    build_permission_denied_details,
    parse_permission_grant,
)
from common.time_utils import local_iso_timestamp
import jira.provider_tools as _jpt  # registers internal Jira tools on import

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8010"))
AGENT_ID = os.environ.get("AGENT_ID", "jira-agent")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://jira:{PORT}")
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://your-org.atlassian.net")
JIRA_API_BASE_URL = os.environ.get("JIRA_API_BASE_URL", f"{JIRA_BASE_URL.rstrip('/')}/rest/api/3")
JIRA_TOKEN = os.environ.get("JIRA_TOKEN", "")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_AUTH_MODE = os.environ.get("JIRA_AUTH_MODE", "basic").strip().lower()
# ORCHESTRATOR_URL removed: boundary agents must not hardcode upstream URLs.
# All A2A callbacks use orchestratorCallbackUrl from message.metadata.
JIRA_CLOUD_ID = os.environ.get("JIRA_CLOUD_ID", "").strip()
CORP_CA_BUNDLE = (
    os.environ.get("CORP_CA_BUNDLE", "") or os.environ.get("SSL_CERT_FILE", "")
)
# Back-end selector: "rest" (default) | "mcp"
JIRA_BACKEND = os.environ.get("JIRA_BACKEND", "rest").strip().lower()

TASK_SEQ = 0
TASKS = {}
TASKS_LOCK = threading.Lock()
TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
TICKET_URL_RE = re.compile(r"(https?://[^\s]+/browse/([A-Z][A-Z0-9]+-\d+))", re.IGNORECASE)
_AGENT_CARD_PATH = os.path.join(os.path.dirname(__file__), "agent-card.json")


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def _make_provider():
    from jira.providers.rest import JiraRESTProvider
    from jira.providers.mcp import JiraMCPProvider

    if JIRA_BACKEND == "mcp":
        print(f"[{AGENT_ID}] Jira back-end: MCP (Atlassian Rovo MCP)")
        return JiraMCPProvider(
            jira_base_url=JIRA_BASE_URL,
            jira_token=JIRA_TOKEN,
            jira_email=JIRA_EMAIL,
            jira_auth_mode=JIRA_AUTH_MODE,
            jira_cloud_id=JIRA_CLOUD_ID,
            jira_api_base_url=JIRA_API_BASE_URL,
            corp_ca_bundle=CORP_CA_BUNDLE,
        )
    print(f"[{AGENT_ID}] Jira back-end: REST API")
    return JiraRESTProvider(
        jira_base_url=JIRA_BASE_URL,
        jira_token=JIRA_TOKEN,
        jira_email=JIRA_EMAIL,
        jira_auth_mode=JIRA_AUTH_MODE,
        jira_cloud_id=JIRA_CLOUD_ID,
        jira_api_base_url=JIRA_API_BASE_URL,
        corp_ca_bundle=CORP_CA_BUNDLE,
    )


PROVIDER = _make_provider()


def _load_agent_card():
    with open(_AGENT_CARD_PATH, encoding="utf-8") as fh:
        card = json.load(fh)
    text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
    return json.loads(text)

def _workspace_headers(handler: BaseHTTPRequestHandler) -> tuple[str, str]:
    workspace_path = (handler.headers.get("X-Shared-Workspace-Path") or "").strip()
    task_id = (handler.headers.get("X-Orchestrator-Task-Id") or "").strip()
    return workspace_path, task_id


def _record_workspace_phase(workspace_path: str, task_id: str, phase: str, **extra):
    record_workspace_stage(
        workspace_path,
        "jira",
        phase,
        task_id=task_id,
        extra={
            "agentId": AGENT_ID,
            "runtimeConfig": {
                "runtime": summarize_runtime_configuration(),
                "backend": JIRA_BACKEND,
            },
            **extra,
        },
    )


def _permission_enforcement_mode() -> str:
    return os.environ.get("PERMISSION_ENFORCEMENT", "strict").strip().lower() or "strict"


def _request_permissions(
    headers=None,
    payload_permissions: dict | None = None,
) -> tuple[dict | None, str]:
    if payload_permissions is not None:
        return payload_permissions, ""
    raw = ((headers or {}).get("X-Task-Permissions") or "").strip()
    if not raw:
        return None, "No permissions attached to request. Explicit permission grant required."
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        return None, "Invalid X-Task-Permissions header. Explicit permission grant required."


def _check_jira_permission(
    *,
    action: str,
    target: str,
    payload_permissions: dict | None = None,
    headers=None,
    message: dict | None = None,
    scope: str = "*",
) -> tuple[bool, str, str]:
    if _permission_enforcement_mode() == "off":
        return True, "allowed", ""

    metadata = (message or {}).get("metadata") or {}
    request_agent = (
        (metadata.get("requestAgent") or "").strip()
        or ((headers or {}).get("X-Request-Agent") or "").strip()
    )
    task_id_hdr = (
        (metadata.get("orchestratorTaskId") or "").strip()
        or ((headers or {}).get("X-Orchestrator-Task-Id") or "").strip()
    )
    permissions_data, missing_reason = _request_permissions(
        headers=headers,
        payload_permissions=(payload_permissions if payload_permissions is not None else metadata.get("permissions")),
    )
    grant = parse_permission_grant(permissions_data)
    if grant:
        allowed, reason = grant.check("jira", action, scope)
        escalation = grant.escalation_for("jira", action, scope)
    else:
        allowed = False
        reason = missing_reason or "No permissions attached to request. Explicit permission grant required."
        escalation = "require_user_approval"

    audit_permission_check(
        task_id=task_id_hdr,
        orchestrator_task_id=task_id_hdr,
        request_agent=request_agent,
        target_agent=AGENT_ID,
        action=action,
        target=target,
        decision="allowed" if allowed else "denied",
        reason=reason,
        agent_id=AGENT_ID,
    )
    return allowed, reason, escalation


def _require_jira_permission(
    *,
    action: str,
    target: str,
    message: dict,
    payload_permissions: dict | None = None,
    scope: str = "*",
) -> None:
    allowed, reason, escalation = _check_jira_permission(
        action=action,
        target=target,
        payload_permissions=payload_permissions,
        message=message,
        scope=scope,
    )
    if allowed:
        return
    if _permission_enforcement_mode() == "strict":
        metadata = (message or {}).get("metadata") or {}
        raise PermissionDeniedError(
            build_permission_denied_details(
                permission_agent="jira",
                target_agent=AGENT_ID,
                action=action,
                target=target,
                reason=reason,
                escalation=escalation or "require_user_approval",
                scope=scope,
                request_agent=str(metadata.get("requestAgent") or "").strip(),
                task_id=str(metadata.get("taskId") or ""),
                orchestrator_task_id=str(metadata.get("orchestratorTaskId") or ""),
            )
        )

    print(
        f"[{AGENT_ID}] WARN: permission check failed but enforcement={_permission_enforcement_mode()}: {reason}"
    )


def _enforce_jira_permission(
    handler: Any,
    *,
    action: str,
    target: str,
    payload_permissions: dict | None = None,
    scope: str = "*",
    response_key: str = "action",
    response_value: str | None = None,
) -> bool:
    allowed, reason, escalation = _check_jira_permission(
        action=action,
        target=target,
        payload_permissions=payload_permissions,
        headers=handler.headers,
        scope=scope,
    )
    if allowed:
        return True
    if _permission_enforcement_mode() == "strict":
        error_body = {
            "error": "permission_denied",
            response_key: response_value or action,
            "reason": reason,
            "escalation": escalation or "require_user_approval",
        }
        handler._send_json(403, error_body)
        return False

    print(
        f"[{AGENT_ID}] WARN: permission check failed but enforcement={_permission_enforcement_mode()}: {reason}"
    )
    return True




def next_task_id():
    global TASK_SEQ
    TASK_SEQ += 1
    return f"jira-task-{TASK_SEQ:04d}"


def _task_message(text):
    return {
        "role": "ROLE_AGENT",
        "parts": [{"text": text}],
    }


def _create_task_record(initial_state, initial_message):
    task_id = next_task_id()
    now = time.time()
    with TASKS_LOCK:
        TASKS[task_id] = {
            "id": task_id,
            "agentId": AGENT_ID,
            "state": initial_state,
            "message": initial_message,
            "artifacts": [],
            "createdAt": now,
            "updatedAt": now,
        }
    return task_id


def _update_task_record(task_id, state=None, message=None, artifacts=None):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return None
        if state is not None:
            task["state"] = state
        if message is not None:
            task["message"] = message
        if artifacts is not None:
            task["artifacts"] = artifacts
        task["updatedAt"] = time.time()
        return dict(task)


def _task_payload(task_id):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return None
        return {
            "id": task["id"],
            "contextId": task["id"],
            "agentId": task["agentId"],
            "status": {
                "state": task["state"],
                "message": _task_message(task["message"]),
            },
            "artifacts": list(task["artifacts"]),
            "createdAt": task["createdAt"],
            "updatedAt": task["updatedAt"],
        }


def _post_json_url(url, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8")
        return response.status, json.loads(raw) if raw.strip() else {}


def _notify_orchestrator_completion(message, downstream_task_id, state, status_text, artifacts):
    """Send A2A callback to orchestrator. Uses orchestratorCallbackUrl from message metadata only.
    No ORCHESTRATOR_URL fallback — boundary agents must not hardcode upstream agent addresses.
    """
    metadata = message.get("metadata", {})
    callback_url = (metadata.get("orchestratorCallbackUrl") or "").strip()
    if not callback_url:
        debug_log(
            AGENT_ID,
            "jira.workflow.callback_skipped",
            taskId=downstream_task_id,
            reason="orchestratorCallbackUrl not set in message metadata",
        )
        return
    try:
        _post_json_url(
            callback_url,
            {
                "taskId": downstream_task_id,
                "downstreamTaskId": downstream_task_id,
                "agentId": AGENT_ID,
                "state": state,
                "statusMessage": status_text,
                "artifacts": artifacts,
            },
        )
    except Exception as error:
        debug_log(
            AGENT_ID,
            "jira.workflow.callback_failed",
            taskId=downstream_task_id,
            callbackUrl=callback_url,
            error=str(error),
        )


def _write_jira_audit(
    *,
    workspace_path: str,
    message: dict,
    operation: str,
    target: str,
    input_summary: dict,
    result: dict,
    duration_ms: int = 0,
) -> None:
    """Append a structured audit entry to audit-log.jsonl in the task workspace."""
    if not workspace_path:
        return
    import json as _json
    metadata = message.get("metadata") or {}
    entry = {
        "ts": local_iso_timestamp(),
        "agentId": AGENT_ID,
        "requestingAgent": str(metadata.get("requestingAgent") or metadata.get("agentId") or ""),
        "requestingTaskId": str(metadata.get("orchestratorTaskId") or ""),
        "operation": operation,
        "target": target,
        "input": input_summary,
        "result": result,
        "durationMs": duration_ms,
    }
    audit_dir = os.path.join(workspace_path, "jira-agent")
    os.makedirs(audit_dir, exist_ok=True)
    audit_path = os.path.join(audit_dir, "audit-log.jsonl")
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")


def _run_task_async(task_id, message):
    metadata = dict(message.get("metadata") or {})
    workspace_path = str(metadata.get("sharedWorkspacePath") or "")
    capability = str(metadata.get("requestedCapability") or "").strip()
    configure_control_tools(
        task_context={
            "taskId": task_id,
            "agentId": AGENT_ID,
            "workspacePath": workspace_path,
            "permissions": metadata.get("permissions"),
        },
        complete_fn=lambda result, artifacts: _update_task_record(
            task_id, state="TASK_STATE_COMPLETED", message=result, artifacts=artifacts or []
        ),
        fail_fn=lambda error: _update_task_record(task_id, state="TASK_STATE_FAILED", message=error),
        input_required_fn=lambda question, ctx: _update_task_record(
            task_id, state="TASK_STATE_INPUT_REQUIRED", message=question
        ),
    )
    _jpt.configure_jira_provider_tools(
        message=message,
        provider=PROVIDER,
        permission_fn=lambda action, target, scope="*": _require_jira_permission(
            action=action, target=target, scope=scope, message=message
        ),
        audit_fn=lambda operation, target, input_summary, result, duration_ms=0: _write_jira_audit(
            workspace_path=workspace_path,
            message=message,
            operation=operation,
            target=target,
            input_summary=input_summary,
            result=result,
            duration_ms=duration_ms,
        ),
    )
    try:
        _update_task_record(
            task_id,
            state="TASK_STATE_WORKING",
            message="Jira agent is processing the task.",
        )
        text = extract_text(message)
        system_prompt = build_system_prompt_from_manifest(os.path.dirname(__file__))
        require_agentic_runtime("Jira Agent")
        get_runtime().run_agentic(
            task=text,
            system_prompt=system_prompt,
            cwd=workspace_path or os.getcwd(),
            tools=[
                "jira_issue_lookup", "jira_search", "jira_get_myself",
                "jira_get_transitions", "jira_validate_permissions",
                "jira_comment", "jira_transition", "jira_assign",
                "jira_create_issue", "jira_update_fields",
                "jira_update_comment", "jira_delete_comment",
                "report_progress", "complete_current_task", "fail_current_task",
                "load_skill",
            ],
            max_turns=15,
            timeout=300,
        )
        # Task state is set by complete_current_task / fail_current_task control tools.
        final = TASKS.get(task_id, {})
        final_state = final.get("state", "TASK_STATE_COMPLETED")
        final_message = final.get("message", "Jira operation completed.")
        final_artifacts = final.get("artifacts", [])
        _notify_orchestrator_completion(
            message, task_id, final_state, final_message, final_artifacts,
        )
    except Exception as error:
        debug_log(AGENT_ID, "jira.workflow.failed", taskId=task_id, error=str(error))
        failure_text = f"Jira agent failed: {error}"
        artifacts = []
        if isinstance(error, PermissionDeniedError):
            artifacts = [build_permission_denied_artifact(error.details, agent_id=AGENT_ID)]
        _update_task_record(
            task_id,
            state="TASK_STATE_FAILED",
            message=failure_text,
            artifacts=artifacts,
        )
        _notify_orchestrator_completion(
            message,
            task_id,
            "TASK_STATE_FAILED",
            failure_text,
            artifacts,
        )


def extract_ticket_key(text):
    match = TICKET_RE.search(text or "")
    return match.group(1) if match else ""


def extract_ticket_url(text):
    match = TICKET_URL_RE.search(text or "")
    return match.group(1).split("?", 1)[0] if match else ""


def _message_workspace_context(message: dict) -> tuple[str, str]:
    metadata = message.get("metadata") or {}
    workspace_path = str(metadata.get("sharedWorkspacePath") or "").strip()
    task_id = str(metadata.get("orchestratorTaskId") or "").strip()
    return workspace_path, task_id




class JiraHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "agent_id": AGENT_ID,
                "backend": PROVIDER.backend_name,
            })
            return
        task_match = re.fullmatch(r"/tasks/([^/]+)", path)
        if task_match:
            task = _task_payload(task_match.group(1))
            if task:
                self._send_json(200, {"task": task})
            else:
                self._send_json(404, {"error": "task_not_found"})
            return
        if path == "/.well-known/agent-card.json":
            self._send_json(200, _load_agent_card())
            return

        # GET /jira/tickets/{key}
        m = re.fullmatch(r"/jira/tickets/([A-Z][A-Z0-9]+-\d+)", path)
        if m:
            key = m.group(1)
            if not _enforce_jira_permission(self, action="read", target=key):
                return
            issue, status = PROVIDER.fetch_issue(key)
            self._send_json(
                200 if status == "fetched" else 502,
                {"ticketKey": key, "status": status, "issue": issue},
            )
            return

        # GET /jira/transitions/{key}
        m = re.fullmatch(r"/jira/transitions/([A-Z][A-Z0-9]+-\d+)", path)
        if m:
            key = m.group(1)
            if not _enforce_jira_permission(self, action="read", target=f"{key}/transitions"):
                return
            transitions, result = PROVIDER.get_transitions(key)
            self._send_json(
                200 if result == "ok" else 502,
                {"ticketKey": key, "result": result, "transitions": transitions},
            )
            return

        # GET /jira/myself
        if path == "/jira/myself":
            if not _enforce_jira_permission(self, action="read", target="myself"):
                return
            user, result = PROVIDER.get_myself()
            workspace_path, task_id = _workspace_headers(self)
            _record_workspace_phase(workspace_path, task_id, "Resolved Jira current user", result=result)
            self._send_json(
                200 if result == "ok" else 502,
                {"result": result, "user": user},
            )
            return

        # GET /jira/search?jql=...&maxResults=10&fields=summary,status
        if path == "/jira/search":
            qs = parse_qs(urlparse(self.path).query)
            jql = (qs.get("jql") or qs.get("q") or [""])[0]
            fields_param = (qs.get("fields") or [""])[0]
            try:
                max_results = int((qs.get("maxResults") or qs.get("max_results") or ["10"])[0])
            except ValueError:
                max_results = 10
            fields = [item.strip() for item in fields_param.split(",") if item.strip()]
            if not _enforce_jira_permission(self, action="read", target=jql or "search"):
                return
            body, result = PROVIDER.search_issues(jql, max_results=max_results, fields=fields or None)
            self._send_json(
                200 if result == "ok" else 502,
                {"result": result, "jql": jql, "search": body},
            )
            return

        # GET /audit?workspace=<path>&taskId=<id>&operation=<op>&since=<ts>
        if path == "/audit":
            qs = parse_qs(urlparse(self.path).query)
            workspace = (qs.get("workspace") or [""])[0]
            task_id_qs = (qs.get("taskId") or [""])[0]
            operation = (qs.get("operation") or [""])[0]
            since = (qs.get("since") or [""])[0]
            if not workspace:
                self._send_json(400, {"error": "workspace parameter required", "note": "Provide ?workspace=<sharedWorkspacePath>"})
                return
            entries = self._read_jira_audit(workspace, task_id=task_id_qs, operation=operation, since=since)
            self._send_json(200, {"entries": entries, "count": len(entries)})
            return

        self._send_json(404, {"error": "not_found"})

    def _read_jira_audit(self, workspace: str, task_id: str = "", operation: str = "", since: str = "") -> list:
        """Read audit-log.jsonl from the given workspace directory."""
        audit_path = os.path.join(workspace, "jira-agent", "audit-log.jsonl")
        if not os.path.isfile(audit_path):
            return []
        entries = []
        with open(audit_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if task_id and entry.get("requestingTaskId") != task_id:
                    continue
                if operation and entry.get("operation") != operation:
                    continue
                if since and entry.get("ts", "") < since:
                    continue
                entries.append(entry)
        return entries

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/message:send":
            body = self._read_body()
            message = body.get("message", {})
            if not message:
                self._send_json(400, {"error": "missing message"})
                return
            task_id = _create_task_record(
                "TASK_STATE_ACCEPTED",
                "Jira agent accepted the task and will continue asynchronously.",
            )
            worker = threading.Thread(
                target=_run_task_async,
                args=(task_id, message),
                daemon=True,
            )
            worker.start()
            self._send_json(200, {"task": _task_payload(task_id)})
            return

        # POST /jira/transitions/{key}  body: {"transition": "In Progress"}
        m = re.fullmatch(r"/jira/transitions/([A-Z][A-Z0-9]+-\d+)", path)
        if m:
            key = m.group(1)
            body = self._read_body()
            name = body.get("transition", "")
            workspace_path, task_id = _workspace_headers(self)
            if not name:
                self._send_json(400, {"error": "missing transition name"})
                return
            if not _enforce_jira_permission(
                self,
                action="transition",
                target=key,
                payload_permissions=body.get("permissions"),
            ):
                return
            tid, result = PROVIDER.transition_issue(key, name)
            _record_workspace_phase(
                workspace_path,
                task_id,
                f"Transitioned Jira ticket {key} to {name}",
                ticketKey=key,
                result=result,
            )
            self._send_json(
                200 if tid else 422,
                {"ticketKey": key, "transitionId": tid, "result": result},
            )
            return

        # POST /jira/comments/{key}  body: {"text": "..."} or {"adf": {...}}
        m = re.fullmatch(r"/jira/comments/([A-Z][A-Z0-9]+-\d+)", path)
        if m:
            key = m.group(1)
            body = self._read_body()
            adf = body.get("adf")
            text = body.get("text", "")
            workspace_path, task_id = _workspace_headers(self)
            if not adf and not text:
                self._send_json(400, {"error": "missing comment text or adf"})
                return
            if not _enforce_jira_permission(
                self,
                action="comment.add",
                target=key,
                payload_permissions=body.get("permissions"),
            ):
                return
            cid, result = PROVIDER.add_comment(key, text, adf_body=adf)
            _record_workspace_phase(
                workspace_path,
                task_id,
                f"Added Jira comment to {key}",
                ticketKey=key,
                commentId=cid,
                result=result,
            )
            self._send_json(
                201 if cid else 502,
                {"ticketKey": key, "commentId": cid, "result": result},
            )
            return

        # POST /jira/tickets
        if path == "/jira/tickets":
            body = self._read_body()
            project_key = body.get("projectKey", "")
            summary = body.get("summary", "")
            issue_type = body.get("issueType", "Task")
            description = body.get("description", "")
            fields = body.get("fields") or body.get("additionalFields") or {}
            if not project_key:
                self._send_json(400, {"error": "missing projectKey"})
                return
            if not summary:
                self._send_json(400, {"error": "missing summary"})
                return
            if not _enforce_jira_permission(
                self,
                action="issue.create",
                target=project_key,
                payload_permissions=body.get("permissions"),
            ):
                return
            issue_body, result = PROVIDER.create_issue(
                project_key, summary, issue_type, description, fields
            )
            issue_key = issue_body.get("key") if isinstance(issue_body, dict) else None
            self._send_json(
                201 if result == "created" else 502,
                {"result": result, "ticketKey": issue_key, "issue": issue_body},
            )
            return

    def do_PUT(self):
        path = urlparse(self.path).path
        # PUT /jira/tickets/{key}
        m = re.fullmatch(r"/jira/tickets/([A-Z][A-Z0-9]+-\d+)", path)
        if m:
            key = m.group(1)
            body = self._read_body()
            fields = body.get("fields") if isinstance(body.get("fields"), dict) else {
                k: v for k, v in body.items() if k not in {"ticketKey", "permissions"}
            }
            if not fields:
                self._send_json(400, {"error": "missing fields"})
                return

            for field_name in fields:
                action = f"issue.update.{field_name}"
                if not _enforce_jira_permission(
                    self,
                    action=action,
                    target=key,
                    payload_permissions=body.get("permissions"),
                    response_key="field",
                    response_value=field_name,
                ):
                    return

            result_body, result = PROVIDER.update_issue_fields(key, fields)
            self._send_json(
                200 if result == "updated" else 502,
                {"ticketKey": key, "result": result, "detail": result_body},
            )
            return

        # PUT /jira/comments/{key}/{comment_id}
        m = re.fullmatch(r"/jira/comments/([A-Z][A-Z0-9]+-\d+)/(\w+)", path)
        if m:
            key, cid_in = m.group(1), m.group(2)
            body = self._read_body()
            text = body.get("text", "")
            if not text:
                self._send_json(400, {"error": "missing comment text"})
                return
            if not _enforce_jira_permission(
                self,
                action="comment.update",
                target=f"{key}/{cid_in}",
                payload_permissions=body.get("permissions"),
                scope="self",
            ):
                return
            cid, result = PROVIDER.update_comment(key, cid_in, text)
            self._send_json(
                200 if cid else 502,
                {"ticketKey": key, "commentId": cid, "result": result},
            )
            return

        # PUT /jira/assignee/{key}
        m = re.fullmatch(r"/jira/assignee/([A-Z][A-Z0-9]+-\d+)", path)
        if m:
            key = m.group(1)
            body = self._read_body()
            workspace_path, task_id = _workspace_headers(self)
            if "accountId" not in body:
                self._send_json(400, {"error": "missing accountId"})
                return
            account_id = body.get("accountId")
            if not _enforce_jira_permission(
                self,
                action="assignee.update",
                target=key,
                payload_permissions=body.get("permissions"),
            ):
                return
            aid, result = PROVIDER.change_assignee(key, account_id)
            _record_workspace_phase(
                workspace_path,
                task_id,
                f"Assigned Jira ticket {key}",
                ticketKey=key,
                accountId=aid,
                result=result,
            )
            self._send_json(
                200 if result == "assigned" else 502,
                {"ticketKey": key, "accountId": aid, "result": result},
            )
            return

        self._send_json(404, {"error": "not_found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        # DELETE /jira/comments/{key}/{comment_id}
        m = re.fullmatch(r"/jira/comments/([A-Z][A-Z0-9]+-\d+)/(\w+)", path)
        if m:
            key, cid_in = m.group(1), m.group(2)
            if not _enforce_jira_permission(
                self,
                action="comment.delete",
                target=f"{key}/{cid_in}",
            ):
                return
            cid, result = PROVIDER.delete_comment(key, cid_in)
            self._send_json(
                200 if cid else 502,
                {"ticketKey": key, "commentId": cid, "result": result},
            )
            return
        self._send_json(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        # Suppress noisy health-check and agent-card polls
        line = args[0] if args else ""
        if any(p in line for p in ("/health", "/.well-known/agent-card.json")):
            return
        print(f"[jira-agent] {line} {args[1] if len(args) > 1 else ''} {args[2] if len(args) > 2 else ''}")


def main():
    print(f"[jira-agent] Jira Agent starting on {HOST}:{PORT} (backend={JIRA_BACKEND})")
    reporter = InstanceReporter(agent_id=AGENT_ID, service_url=ADVERTISED_URL, port=PORT)
    reporter.start()
    server = ThreadingHTTPServer((HOST, PORT), JiraHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()