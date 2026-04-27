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
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from common.devlog import debug_log, preview_data, record_workspace_stage
from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.message_utils import build_text_artifact, extract_text
from common.rules_loader import build_system_prompt
from common.runtime.adapter import get_runtime, summarize_runtime_configuration
from common.time_utils import local_iso_timestamp
from jira import prompts

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
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:8080")
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
_SKILL_GUIDE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    ".github",
    "skills",
    "jira-cloud-workflow",
    "SKILL.md",
)


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def _make_provider():
    from jira.providers.rest import JiraRESTProvider
    from jira.providers.mcp import JiraMCPProvider

    kwargs = dict(
        jira_base_url=JIRA_BASE_URL,
        jira_token=JIRA_TOKEN,
        jira_email=JIRA_EMAIL,
        jira_auth_mode=JIRA_AUTH_MODE,
        jira_cloud_id=JIRA_CLOUD_ID,
        jira_api_base_url=JIRA_API_BASE_URL,
        corp_ca_bundle=CORP_CA_BUNDLE,
    )
    if JIRA_BACKEND == "mcp":
        print(f"[{AGENT_ID}] Jira back-end: MCP (Atlassian Rovo MCP)")
        return JiraMCPProvider(**kwargs)
    print(f"[{AGENT_ID}] Jira back-end: REST API")
    return JiraRESTProvider(**kwargs)


PROVIDER = _make_provider()


def _load_agent_card():
    with open(_AGENT_CARD_PATH, encoding="utf-8") as fh:
        card = json.load(fh)
    text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
    return json.loads(text)


def _read_text_file(path):
    if not path or not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _strip_frontmatter(text):
    stripped = (text or "").strip()
    if not stripped.startswith("---\n"):
        return stripped
    parts = stripped.split("\n---\n", 1)
    if len(parts) == 2:
        return parts[1].strip()
    return stripped


def _load_skill_guide(limit=2200):
    text = _strip_frontmatter(_read_text_file(_SKILL_GUIDE_PATH))
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


def _run_agentic(
    prompt: str,
    actor: str,
    *,
    system_prompt: str | None = None,
    context: dict | None = None,
    timeout: int = 120,
    max_tokens: int = 4096,
) -> str:
    result = get_runtime().run(
        prompt=prompt,
        context=context,
        system_prompt=system_prompt,
        timeout=timeout,
        max_tokens=max_tokens,
    )
    for warning in result.get("warnings") or []:
        print(f"[{AGENT_ID}] Runtime warning ({actor}): {warning}")
    return result.get("raw_response") or result.get("summary") or ""


def _write_workspace_file(workspace_path, relative_name, content):
    if not workspace_path:
        return
    os.makedirs(workspace_path, exist_ok=True)
    target_path = os.path.join(workspace_path, relative_name)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as handle:
        handle.write(content)


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
    metadata = message.get("metadata", {})
    callback_url = (metadata.get("orchestratorCallbackUrl") or "").strip()
    if not callback_url:
        orchestrator_task_id = (metadata.get("orchestratorTaskId") or "").strip()
        if not orchestrator_task_id:
            return
        callback_url = f"{ORCHESTRATOR_URL.rstrip('/')}/tasks/{orchestrator_task_id}/callbacks"
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


def _run_task_async(task_id, message):
    try:
        _update_task_record(
            task_id,
            state="TASK_STATE_WORKING",
            message="Jira agent is processing the task.",
        )
        status_text, artifacts = process_message(message)
        _update_task_record(
            task_id,
            state="TASK_STATE_COMPLETED",
            message=status_text,
            artifacts=artifacts,
        )
        _notify_orchestrator_completion(
            message,
            task_id,
            "TASK_STATE_COMPLETED",
            status_text,
            artifacts,
        )
    except Exception as error:
        debug_log(AGENT_ID, "jira.workflow.failed", taskId=task_id, error=str(error))
        failure_text = f"Jira agent failed: {error}"
        _update_task_record(
            task_id,
            state="TASK_STATE_FAILED",
            message=failure_text,
            artifacts=[],
        )
        _notify_orchestrator_completion(
            message,
            task_id,
            "TASK_STATE_FAILED",
            failure_text,
            [],
        )


def extract_ticket_key(text):
    match = TICKET_RE.search(text or "")
    return match.group(1) if match else ""


def extract_ticket_url(text):
    match = TICKET_URL_RE.search(text or "")
    return match.group(1).split("?", 1)[0] if match else ""


def process_message(message):
    user_text = extract_text(message)
    metadata = message.get("metadata", {})
    workspace_path = (metadata.get("sharedWorkspacePath") or "").strip()
    task_id = (metadata.get("orchestratorTaskId") or "").strip()
    trusted_ticket_key = (metadata.get("ticketKey") or "").strip()
    browse_url = (
        metadata.get("ticketUrl")
        or metadata.get("browseUrl")
        or extract_ticket_url(user_text)
    )
    ticket_key = trusted_ticket_key or extract_ticket_key(browse_url) or extract_ticket_key(user_text)
    browse_url = (browse_url or "").strip()
    skill_guide = _load_skill_guide()
    debug_log(AGENT_ID, "jira.message.received", ticketKey=ticket_key, userText=user_text)
    _record_workspace_phase(workspace_path, task_id, "Received Jira request", ticketKey=ticket_key)
    issue_payload = None
    fetch_status = "missing_explicit_ticket_url"
    if ticket_key:
        issue_payload, fetch_status = PROVIDER.fetch_issue(ticket_key)
        _record_workspace_phase(
            workspace_path,
            task_id,
            f"Fetched Jira ticket {ticket_key}",
            ticketKey=ticket_key,
            fetchStatus=fetch_status,
        )

    prompt = prompts.SUMMARY_TEMPLATE.format(
        skill_guide=skill_guide or "No local skill guide loaded.",
        user_text=user_text,
        ticket_key=ticket_key or "none",
        browse_url=browse_url or "n/a",
        fetch_status=fetch_status,
        issue_payload=(
            json.dumps(issue_payload, ensure_ascii=False, indent=2)
            if issue_payload else "No issue payload fetched."
        ),
    )

    summary = _run_agentic(
        prompt,
        "Jira Agent",
        system_prompt=build_system_prompt(prompts.SUMMARY_SYSTEM, "jira"),
    )
    if not ticket_key:
        summary = (
            "No explicit Jira browse URL was found in the request. "
            "Provide the full Jira ticket URL so the Jira agent can fetch the issue safely.\n\n"
            f"LLM summary:\n{summary}"
        )
    elif not browse_url and not trusted_ticket_key:
        summary = (
            "No explicit Jira browse URL was found in the request, so the Jira agent did not fabricate "
            "one from configuration. Provide the full Jira ticket URL to continue safely.\n\n"
            f"LLM summary:\n{summary}"
        )

    artifacts = [
        build_text_artifact(
            "jira-summary",
            summary,
            artifact_type="application/vnd.multi-agent.summary",
            metadata={
                "agentId": AGENT_ID,
                "ticketKey": ticket_key,
                "browseUrl": browse_url,
                "ticketUrl": browse_url,
                "fetchStatus": fetch_status,
            },
        )
    ]

    if issue_payload:
        artifacts.append(
            build_text_artifact(
                "jira-raw-payload",
                json.dumps(issue_payload, ensure_ascii=False, indent=2),
                artifact_type="application/json",
                metadata={
                    "agentId": AGENT_ID,
                    "ticketKey": ticket_key,
                    "browseUrl": browse_url,
                    "ticketUrl": browse_url,
                },
            )
        )

    if workspace_path:
        _write_workspace_file(workspace_path, "jira/jira-summary.md", summary)
        if issue_payload:
            _write_workspace_file(
                workspace_path,
                "jira/jira-issue.json",
                json.dumps(issue_payload, ensure_ascii=False, indent=2),
            )
            attachments = ((issue_payload or {}).get("fields") or {}).get("attachment") or []
            if isinstance(attachments, list) and attachments:
                _write_workspace_file(
                    workspace_path,
                    "jira/jira-attachments.json",
                    json.dumps(attachments, ensure_ascii=False, indent=2),
                )

    status_text = f"Jira analysis completed for {ticket_key or 'request without ticket key'}."
    _record_workspace_phase(
        workspace_path,
        task_id,
        "Completed Jira request",
        ticketKey=ticket_key,
        fetchStatus=fetch_status,
        updatedAt=local_iso_timestamp(),
    )
    debug_log(
        AGENT_ID, "jira.message.completed",
        ticketKey=ticket_key, fetchStatus=fetch_status, browseUrl=browse_url,
    )
    return status_text, artifacts


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
            transitions, result = PROVIDER.get_transitions(key)
            self._send_json(
                200 if result == "ok" else 502,
                {"ticketKey": key, "result": result, "transitions": transitions},
            )
            return

        # GET /jira/myself
        if path == "/jira/myself":
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
            body, result = PROVIDER.search_issues(jql, max_results=max_results, fields=fields or None)
            self._send_json(
                200 if result == "ok" else 502,
                {"result": result, "jql": jql, "search": body},
            )
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/message:send":
            body = self._read_body()
            message = body.get("message", {})
            if not message:
                self._send_json(400, {"error": "missing message"})
                return
            configuration = body.get("configuration") or {}
            if configuration.get("returnImmediately"):
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
            status_text, artifacts = process_message(message)
            self._send_json(200, {
                "task": {
                    "id": next_task_id(), "agentId": AGENT_ID,
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"text": status_text}]},
                    },
                    "artifacts": artifacts,
                }
            })
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
                k: v for k, v in body.items() if k not in {"ticketKey"}
            }
            if not fields:
                self._send_json(400, {"error": "missing fields"})
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