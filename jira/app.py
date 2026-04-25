"""Long-running Jira agent — ticket fetch, transitions, and comment CRUD."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
import os
import re
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from common.devlog import debug_log, preview_data
from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.llm_client import generate_text
from common.message_utils import build_text_artifact, extract_text

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
_DISCOVERED_CLOUD_ID = JIRA_CLOUD_ID


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


def _write_workspace_file(workspace_path, relative_name, content):
    if not workspace_path:
        return
    os.makedirs(workspace_path, exist_ok=True)
    target_path = os.path.join(workspace_path, relative_name)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _ssl_ctx():
    ctx = ssl.create_default_context()
    if CORP_CA_BUNDLE and os.path.isfile(CORP_CA_BUNDLE):
        ctx.load_verify_locations(CORP_CA_BUNDLE)
    return ctx


def _jira_auth_header():
    """Build the Jira Authorization header from env configuration."""
    token = (JIRA_TOKEN or "").strip()
    if not token:
        return None

    if token.lower().startswith(("basic ", "bearer ")):
        return token

    use_basic = JIRA_AUTH_MODE == "basic" or (
        JIRA_AUTH_MODE == "auto" and bool(JIRA_EMAIL.strip())
    )
    if use_basic:
        user = JIRA_EMAIL.strip()
        if not user:
            return None
        basic_token = base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
        return f"Basic {basic_token}"

    return f"Bearer {token}"


def _looks_like_atlassian_cloud_site(url):
    netloc = urlparse(url or "").netloc.lower()
    return netloc.endswith(".atlassian.net")


def _discover_cloud_id():
    global _DISCOVERED_CLOUD_ID

    if _DISCOVERED_CLOUD_ID:
        return _DISCOVERED_CLOUD_ID
    if not _looks_like_atlassian_cloud_site(JIRA_BASE_URL):
        return ""

    request = Request(
        f"{JIRA_BASE_URL.rstrip('/')}/_edge/tenant_info",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=10, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8")
            body = json.loads(raw) if raw.strip() else {}
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return ""

    cloud_id = str(body.get("cloudId") or body.get("cloudid") or "").strip()
    if cloud_id:
        _DISCOVERED_CLOUD_ID = cloud_id
    return _DISCOVERED_CLOUD_ID


def _scoped_api_base_url():
    cloud_id = _discover_cloud_id()
    if not cloud_id:
        return ""
    return f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"


def _candidate_api_base_urls():
    primary = JIRA_API_BASE_URL.rstrip("/")
    candidates = []
    if _looks_like_atlassian_cloud_site(primary):
        scoped = _scoped_api_base_url()
        if scoped:
            candidates.append(scoped)
    if primary and primary not in candidates:
        candidates.append(primary)
    return candidates


def _jira_request_once(api_base_url, method, path, payload=None):
    url = f"{api_base_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    auth_header = _jira_auth_header()
    if auth_header:
        headers["Authorization"] = auth_header
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8")
            body = json.loads(raw) if raw.strip() else {}
            return resp.status, body
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"error": raw[:500]}
        return exc.code, body
    except URLError as exc:
        return 0, {"error": str(exc.reason)}


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


def _jira_request(method, path, payload=None):
    """Generic Jira REST API call. Returns (http_status, body_dict)."""
    last_status, last_body = 0, {}
    candidates = _candidate_api_base_urls()
    for index, api_base_url in enumerate(candidates):
        status, body = _jira_request_once(api_base_url, method, path, payload)
        last_status, last_body = status, body
        should_retry_scoped = (
            index == 0
            and len(candidates) > 1
            and status in (401, 403, 404)
        )
        if not should_retry_scoped:
            return status, body
        debug_log(
            AGENT_ID,
            "jira.auth.retry_scoped_gateway",
            apiBaseUrl=api_base_url,
            path=path,
            status=status,
        )
    return last_status, last_body


def _text_to_adf(text):
    value = str(text or "").strip()
    if not value:
        return {"type": "doc", "version": 1, "content": []}
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": value}],
            }
        ],
    }


def _normalize_issue_fields(fields):
    normalized = dict(fields) if isinstance(fields, dict) else {}
    if isinstance(normalized.get("description"), str):
        normalized["description"] = _text_to_adf(normalized["description"])
    return normalized


def _ticket_urls(ticket_key, browse_url=""):
    if not ticket_key:
        return browse_url or "", ""
    api_candidates = _candidate_api_base_urls()
    api_base_url = api_candidates[-1] if api_candidates else JIRA_API_BASE_URL.rstrip("/")
    return (
        (browse_url or "").strip(),
        f"{api_base_url}/issue/{ticket_key}",
    )


def _fetch_issue(ticket_key, browse_url=""):
    if not ticket_key:
        return None, "no_ticket_key"
    browse_url, _ = _ticket_urls(ticket_key, browse_url=browse_url)
    debug_log(AGENT_ID, "jira.ticket.fetch.start",
              ticketKey=ticket_key, browseUrl=browse_url)
    status, body = _jira_request("GET", f"issue/{ticket_key}")
    if status == 200:
        debug_log(AGENT_ID, "jira.ticket.fetch.success",
                  ticketKey=ticket_key, body=preview_data(body))
        return body, "fetched"
    debug_log(AGENT_ID, "jira.ticket.fetch.error",
              ticketKey=ticket_key, status=status, body=preview_data(body))
    return body, "fetch_failed"


def _get_transitions(ticket_key):
    if not ticket_key:
        return [], "no_ticket_key"
    status, body = _jira_request("GET", f"issue/{ticket_key}/transitions")
    if status == 200:
        return body.get("transitions", []), "ok"
    return [], f"error_{status}"


def _transition_issue(ticket_key, transition_name):
    transitions, result = _get_transitions(ticket_key)
    if result != "ok":
        return None, f"could_not_fetch_transitions: {result}"
    target_lower = transition_name.strip().lower()
    match = None
    for t in transitions:
        if not isinstance(t, dict):
            continue
        name = t.get("name", "")
        if name.lower() == target_lower or name.lower().startswith(target_lower):
            match = t
            break
    if not match:
        available = [t.get("name") for t in transitions if isinstance(t, dict)]
        debug_log(AGENT_ID, "jira.ticket.transition.not_found",
                  ticketKey=ticket_key, target=transition_name, available=available)
        return None, f"transition_not_found (available: {available})"
    tid = match.get("id")
    transition_label = match.get("name", transition_name)
    if not tid:
        return None, "transition_missing_id"
    debug_log(AGENT_ID, "jira.ticket.transition.apply",
              ticketKey=ticket_key, transitionId=tid, transitionName=transition_label)
    status, body = _jira_request(
        "POST", f"issue/{ticket_key}/transitions", {"transition": {"id": tid}}
    )
    if status in (200, 204):
        debug_log(AGENT_ID, "jira.ticket.transition.success",
                  ticketKey=ticket_key, transitionName=transition_label)
        return tid, f"transitioned_to:{transition_label}"
    debug_log(AGENT_ID, "jira.ticket.transition.error",
              ticketKey=ticket_key, status=status, body=preview_data(body))
    return None, f"transition_failed_{status}"


def _add_comment(ticket_key, text, adf_body=None):
    """Post a comment. If adf_body is provided, use it directly as ADF; otherwise wrap text in ADF."""
    if adf_body and isinstance(adf_body, dict):
        body_content = adf_body
    else:
        body_content = {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": str(text or "")}]}],
        }
    payload = {"body": body_content}
    status, body = _jira_request("POST", f"issue/{ticket_key}/comment", payload)
    if status == 201:
        comment_id = body.get("id", "")
        debug_log(AGENT_ID, "jira.comment.added",
                  ticketKey=ticket_key, commentId=comment_id)
        return comment_id, "added"
    debug_log(AGENT_ID, "jira.comment.add_error",
              ticketKey=ticket_key, status=status, body=preview_data(body))
    return None, f"add_failed_{status}"


def _update_comment(ticket_key, comment_id, new_text, adf_body=None):
    if adf_body and isinstance(adf_body, dict):
        body_content = adf_body
    else:
        body_content = {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": str(new_text or "")}]}],
        }
    payload = {"body": body_content}
    status, body = _jira_request(
        "PUT", f"issue/{ticket_key}/comment/{comment_id}", payload
    )
    if status == 200:
        debug_log(AGENT_ID, "jira.comment.updated",
                  ticketKey=ticket_key, commentId=comment_id)
        return comment_id, "updated"
    debug_log(AGENT_ID, "jira.comment.update_error",
              ticketKey=ticket_key, commentId=comment_id,
              status=status, body=preview_data(body))
    return None, f"update_failed_{status}"


def _delete_comment(ticket_key, comment_id):
    status, body = _jira_request(
        "DELETE", f"issue/{ticket_key}/comment/{comment_id}"
    )
    if status in (200, 204):
        debug_log(AGENT_ID, "jira.comment.deleted",
                  ticketKey=ticket_key, commentId=comment_id)
        return comment_id, "deleted"
    debug_log(AGENT_ID, "jira.comment.delete_error",
              ticketKey=ticket_key, commentId=comment_id,
              status=status, body=preview_data(body))
    return None, f"delete_failed_{status}"


def _get_myself():
    """Return the authenticated user's account info."""
    status, body = _jira_request("GET", "myself")
    if status == 200:
        return body, "ok"
    return body, f"error_{status}"


def _search_issues(jql, max_results=10, fields=None):
    if not jql:
        return {"error": "missing_jql"}, "missing_jql"
    payload = {"jql": jql, "maxResults": max(1, min(int(max_results or 10), 100))}
    if fields:
        payload["fields"] = fields
    status, body = _jira_request("POST", "search/jql", payload)
    if status == 200:
        return body, "ok"
    return body, f"error_{status}"


def _create_issue(project_key, summary, issue_type, description="", fields=None):
    payload_fields = _normalize_issue_fields(fields)
    payload_fields.setdefault("project", {"key": project_key})
    payload_fields.setdefault("summary", summary)
    payload_fields.setdefault("issuetype", {"name": issue_type})
    if description and "description" not in payload_fields:
        payload_fields["description"] = _text_to_adf(description)
    status, body = _jira_request("POST", "issue", {"fields": payload_fields})
    if status == 201:
        return body, "created"
    return body, f"create_failed_{status}"


def _update_issue_fields(ticket_key, fields):
    payload_fields = _normalize_issue_fields(fields)
    if not payload_fields:
        return None, "missing_fields"
    status, body = _jira_request("PUT", f"issue/{ticket_key}", {"fields": payload_fields})
    if status in (200, 204):
        return {"ticketKey": ticket_key}, "updated"
    return body, f"update_failed_{status}"


def _change_assignee(ticket_key, account_id):
    """Assign ticket to the given Atlassian account ID. Pass None to unassign."""
    payload = {"accountId": account_id}
    status, body = _jira_request(
        "PUT", f"issue/{ticket_key}/assignee", payload
    )
    if status in (200, 204):
        debug_log(AGENT_ID, "jira.assignee.changed",
                  ticketKey=ticket_key, accountId=account_id)
        return account_id, "assigned"
    debug_log(AGENT_ID, "jira.assignee.change_error",
              ticketKey=ticket_key, accountId=account_id,
              status=status, body=preview_data(body))
    return None, f"assignee_failed_{status}"


def process_message(message):
    user_text = extract_text(message)
    metadata = message.get("metadata", {})
    workspace_path = (metadata.get("sharedWorkspacePath") or "").strip()
    trusted_ticket_key = (metadata.get("ticketKey") or "").strip()
    browse_url = (
        metadata.get("ticketUrl")
        or metadata.get("browseUrl")
        or extract_ticket_url(user_text)
    )
    ticket_key = trusted_ticket_key or extract_ticket_key(browse_url) or extract_ticket_key(user_text)
    browse_url, _ = _ticket_urls(ticket_key, browse_url=browse_url)
    skill_guide = _load_skill_guide()
    debug_log(
        AGENT_ID,
        "jira.message.received",
        ticketKey=ticket_key,
        userText=user_text,
    )
    issue_payload = None
    fetch_status = "missing_explicit_ticket_url"
    if ticket_key:
        issue_payload, fetch_status = _fetch_issue(ticket_key, browse_url=browse_url)

    prompt = f"""
You are the Jira Agent in a Constellation multi-agent software delivery system.
Summarize the Jira request for downstream engineering agents.

Operational skill guide:
{skill_guide or 'No local skill guide loaded.'}

User request:
{user_text}

Detected ticket key: {ticket_key or 'none'}
Ticket browse URL: {browse_url or 'n/a'}
Fetch status: {fetch_status}
Issue payload:
{json.dumps(issue_payload, ensure_ascii=False, indent=2) if issue_payload else 'No issue payload fetched.'}

Return a concise operator-facing summary with these sections:
1. Ticket
2. What matters
3. Recommended next engineering step
""".strip()

    summary = generate_text(prompt, "Jira Agent")
    if not ticket_key:
        summary = (
            "No explicit Jira browse URL was found in the request. "
            "Provide the full Jira ticket URL so the Jira agent can fetch the issue safely.\n\n"
            f"LLM summary:\n{summary}"
        )
    elif not browse_url and not trusted_ticket_key:
        summary = (
            "No explicit Jira browse URL was found in the request, so the Jira agent did not fabricate one from configuration. "
            "Provide the full Jira ticket URL to continue safely.\n\n"
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
    debug_log(
        AGENT_ID,
        "jira.message.completed",
        ticketKey=ticket_key,
        fetchStatus=fetch_status,
        browseUrl=browse_url,
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
            self._send_json(200, {"status": "ok", "agent_id": AGENT_ID})
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
            payload, status = _fetch_issue(key)
            self._send_json(200 if status == "fetched" else 502,
                            {"ticketKey": key, "status": status, "issue": payload})
            return

        # GET /jira/transitions/{key}
        m = re.fullmatch(r"/jira/transitions/([A-Z][A-Z0-9]+-\d+)", path)
        if m:
            key = m.group(1)
            transitions, result = _get_transitions(key)
            self._send_json(200 if result == "ok" else 502,
                            {"ticketKey": key, "result": result, "transitions": transitions})
            return

        # GET /jira/myself
        if path == "/jira/myself":
            body, result = _get_myself()
            self._send_json(200 if result == "ok" else 502,
                            {"result": result, "user": body})
            return

        # GET /jira/search?jql=key=DMPP-2647&maxResults=10&fields=summary,status
        if path == "/jira/search":
            qs = parse_qs(urlparse(self.path).query)
            jql = (qs.get("jql") or qs.get("q") or [""])[0]
            fields_param = (qs.get("fields") or [""])[0]
            try:
                max_results = int((qs.get("maxResults") or qs.get("max_results") or ["10"])[0])
            except ValueError:
                max_results = 10
            fields = [item.strip() for item in fields_param.split(",") if item.strip()]
            body, result = _search_issues(jql, max_results=max_results, fields=fields or None)
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
            if not name:
                self._send_json(400, {"error": "missing transition name"})
                return
            tid, result = _transition_issue(key, name)
            self._send_json(200 if tid else 422,
                            {"ticketKey": key, "transitionId": tid, "result": result})
            return

        # POST /jira/comments/{key}  body: {"text": "..."} or {"adf": {...}}
        m = re.fullmatch(r"/jira/comments/([A-Z][A-Z0-9]+-\d+)", path)
        if m:
            key = m.group(1)
            body = self._read_body()
            adf = body.get("adf")
            text = body.get("text", "")
            if not adf and not text:
                self._send_json(400, {"error": "missing comment text or adf"})
                return
            cid, result = _add_comment(key, text, adf_body=adf)
            self._send_json(201 if cid else 502,
                            {"ticketKey": key, "commentId": cid, "result": result})
            return

        # POST /jira/tickets  body: {"projectKey":"DMPP","summary":"...","issueType":"Task"}
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
            issue_body, result = _create_issue(project_key, summary, issue_type, description, fields)
            issue_key = issue_body.get("key") if isinstance(issue_body, dict) else None
            self._send_json(
                201 if result == "created" else 502,
                {"result": result, "ticketKey": issue_key, "issue": issue_body},
            )
            return

    def do_PUT(self):
        path = urlparse(self.path).path
        # PUT /jira/tickets/{key}  body: {"fields": {"summary": "..."}}
        m = re.fullmatch(r"/jira/tickets/([A-Z][A-Z0-9]+-\d+)", path)
        if m:
            key = m.group(1)
            body = self._read_body()
            fields = body.get("fields") if isinstance(body.get("fields"), dict) else {
                field_name: value
                for field_name, value in body.items()
                if field_name not in {"ticketKey"}
            }
            if not fields:
                self._send_json(400, {"error": "missing fields"})
                return
            result_body, result = _update_issue_fields(key, fields)
            self._send_json(
                200 if result == "updated" else 502,
                {"ticketKey": key, "result": result, "detail": result_body},
            )
            return

        # PUT /jira/comments/{key}/{comment_id}  body: {"text": "..."}
        m = re.fullmatch(r"/jira/comments/([A-Z][A-Z0-9]+-\d+)/(\w+)", path)
        if m:
            key, cid_in = m.group(1), m.group(2)
            body = self._read_body()
            text = body.get("text", "")
            if not text:
                self._send_json(400, {"error": "missing comment text"})
                return
            cid, result = _update_comment(key, cid_in, text)
            self._send_json(200 if cid else 502,
                            {"ticketKey": key, "commentId": cid, "result": result})
            return

        # PUT /jira/assignee/{key}  body: {"accountId": "..."}
        m = re.fullmatch(r"/jira/assignee/([A-Z][A-Z0-9]+-\d+)", path)
        if m:
            key = m.group(1)
            body = self._read_body()
            if "accountId" not in body:
                self._send_json(400, {"error": "missing accountId"})
                return
            account_id = body.get("accountId")
            aid, result = _change_assignee(key, account_id)
            self._send_json(200 if result == "assigned" else 502,
                            {"ticketKey": key, "accountId": aid, "result": result})
            return

        self._send_json(404, {"error": "not_found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        # DELETE /jira/comments/{key}/{comment_id}
        m = re.fullmatch(r"/jira/comments/([A-Z][A-Z0-9]+-\d+)/(\w+)", path)
        if m:
            key, cid_in = m.group(1), m.group(2)
            cid, result = _delete_comment(key, cid_in)
            self._send_json(200 if cid else 502,
                            {"ticketKey": key, "commentId": cid, "result": result})
            return
        self._send_json(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        # Suppress noisy health-check and agent-card polls
        line = args[0] if args else ""
        if any(p in line for p in ("/health", "/.well-known/agent-card.json")):
            return
        print(f"[jira-agent] {line} {args[1] if len(args) > 1 else ''} {args[2] if len(args) > 2 else ''}")


def main():
    print(f"[jira-agent] Jira Agent starting on {HOST}:{PORT}")
    reporter = InstanceReporter(agent_id=AGENT_ID, service_url=ADVERTISED_URL, port=PORT)
    reporter.start()
    server = ThreadingHTTPServer((HOST, PORT), JiraHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()