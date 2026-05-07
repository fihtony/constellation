"""UI Design Agent — Figma (REST API) + Google Stitch (MCP) design data access."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

from common.devlog import record_workspace_stage
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

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Add this agent's own directory to sys.path so figma_client and stitch_client
# can be imported as local modules (they are specific to ui-design, not shared).
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

import figma_client  # noqa: E402  (local to ui-design/)
import stitch_client  # noqa: E402  (local to ui-design/)
import provider_tools as _udt  # noqa: E402 — registers ui-design internal tools

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8040"))
AGENT_ID = os.environ.get("AGENT_ID", "ui-design-agent")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://ui-design:{PORT}")

_AGENT_CARD_PATH = os.path.join(os.path.dirname(__file__), "agent-card.json")
_TASK_SEQ = 0
_TASK_SEQ_LOCK = threading.Lock()
_TASKS: dict[str, dict] = {}


def _runtime_config_summary() -> dict:
    return {
        "runtime": summarize_runtime_configuration(),
        "provider": "figma+stitch",
    }


def _permission_enforcement_mode() -> str:
    return os.environ.get("PERMISSION_ENFORCEMENT", "strict").strip().lower() or "strict"


def _request_permissions(headers=None, permissions_data: dict | None = None) -> tuple[dict | None, str]:
    if permissions_data is not None:
        return permissions_data, ""
    raw = ((headers or {}).get("X-Task-Permissions") or "").strip() if headers else ""
    if not raw:
        return None, "No permissions attached to request. Explicit permission grant required."
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        return None, "Invalid X-Task-Permissions header. Explicit permission grant required."


def _check_ui_permission(
    *,
    action: str,
    target: str,
    message: dict | None = None,
    headers=None,
    permissions_data: dict | None = None,
) -> tuple[bool, str, str]:
    if _permission_enforcement_mode() == "off":
        return True, "allowed", ""

    metadata = (message or {}).get("metadata") or {}
    request_agent = (
        (metadata.get("requestAgent") or "").strip()
        or ((headers or {}).get("X-Request-Agent") or "").strip()
    )
    task_id = (
        (metadata.get("orchestratorTaskId") or "").strip()
        or ((headers or {}).get("X-Orchestrator-Task-Id") or "").strip()
    )
    raw_permissions, missing_reason = _request_permissions(
        headers=headers,
        permissions_data=permissions_data if permissions_data is not None else metadata.get("permissions"),
    )
    grant = parse_permission_grant(raw_permissions)
    if grant:
        allowed, reason = grant.check("ui-design", action)
        escalation = grant.escalation_for("ui-design", action)
    else:
        allowed = False
        reason = missing_reason or "No permissions attached to request. Explicit permission grant required."
        escalation = "require_user_approval"

    audit_permission_check(
        task_id=task_id,
        orchestrator_task_id=task_id,
        request_agent=request_agent,
        target_agent=AGENT_ID,
        action=action,
        target=target,
        decision="allowed" if allowed else "denied",
        reason=reason,
        agent_id=AGENT_ID,
    )
    return allowed, reason, escalation


def _require_ui_permission(*, action: str, target: str, message: dict) -> None:
    allowed, reason, escalation = _check_ui_permission(action=action, target=target, message=message)
    if allowed:
        return
    if _permission_enforcement_mode() == "strict":
        metadata = (message or {}).get("metadata") or {}
        raise PermissionDeniedError(
            build_permission_denied_details(
                permission_agent="ui-design",
                target_agent=AGENT_ID,
                action=action,
                target=target,
                reason=reason,
                escalation=escalation or "require_user_approval",
                request_agent=str(metadata.get("requestAgent") or "").strip(),
                task_id=str(metadata.get("taskId") or ""),
                orchestrator_task_id=str(metadata.get("orchestratorTaskId") or ""),
            )
        )
    print(f"[{AGENT_ID}] WARN: permission check failed but enforcement={_permission_enforcement_mode()}: {reason}")


def _enforce_http_ui_permission(handler: Any, *, action: str, target: str) -> bool:
    allowed, reason, escalation = _check_ui_permission(action=action, target=target, headers=handler.headers)
    if allowed:
        return True
    if _permission_enforcement_mode() == "strict":
        handler._send_json(
            403,
            {
                "error": "permission_denied",
                "action": action,
                "reason": reason,
                "escalation": escalation or "require_user_approval",
            },
        )
        return False
    print(f"[{AGENT_ID}] WARN: permission check failed but enforcement={_permission_enforcement_mode()}: {reason}")
    return True


def _load_agent_card() -> dict:
    with open(_AGENT_CARD_PATH, encoding="utf-8") as fh:
        card = json.load(fh)
    text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
    return json.loads(text)


def _next_task_id() -> str:
    global _TASK_SEQ
    with _TASK_SEQ_LOCK:
        _TASK_SEQ += 1
        return f"ui-design-task-{_TASK_SEQ:04d}"



# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------

def _fire_callback(
    callback_url: str, task_id: str, state: str,
    status_message: str, artifacts: list,
) -> None:
    from urllib.request import Request as Req, urlopen as _urlopen

    payload = {
        "downstreamTaskId": task_id,
        "state": state,
        "statusMessage": status_message,
        "artifacts": artifacts,
        "agentId": AGENT_ID,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Req(
        callback_url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with _urlopen(req, timeout=10):
            pass
    except Exception as exc:
        print(f"[{AGENT_ID}] Callback failed: {exc}", flush=True)


def _update_task(task_id: str, *, state: str, message: str = "", artifacts: list | None = None) -> None:
    """Thread-safe task state update."""
    with _TASK_SEQ_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task["status"] = {
            "state": state,
            "message": {"role": "ROLE_AGENT", "parts": [{"text": message}]},
        }
        if artifacts is not None:
            task["artifacts"] = artifacts


def _write_ui_design_audit(
    *,
    workspace_path: str,
    message: dict,
    operation: str,
    target: str,
    result: dict,
    duration_ms: int = 0,
) -> None:
    """Append a structured audit entry to audit-log.jsonl in the task workspace."""
    if not workspace_path:
        return
    from common.time_utils import local_iso_timestamp
    metadata = message.get("metadata") or {}
    entry = {
        "ts": local_iso_timestamp(),
        "agentId": AGENT_ID,
        "requestingAgent": str(metadata.get("requestingAgent") or metadata.get("agentId") or ""),
        "requestingTaskId": str(metadata.get("orchestratorTaskId") or ""),
        "operation": operation,
        "target": target,
        "result": result,
        "durationMs": duration_ms,
    }
    audit_dir = os.path.join(workspace_path, "ui-design-agent")
    os.makedirs(audit_dir, exist_ok=True)
    audit_path = os.path.join(audit_dir, "audit-log.jsonl")
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _run_task_background(
    task_id: str, message: dict,
) -> None:
    metadata = message.get("metadata", {}) if isinstance(message, dict) else {}
    workspace_path = metadata.get("sharedWorkspacePath", "")
    capability = metadata.get("requestedCapability", "")
    configure_control_tools(
        task_context={
            "taskId": task_id,
            "agentId": AGENT_ID,
            "workspacePath": workspace_path,
            "permissions": metadata.get("permissions"),
        },
        complete_fn=lambda result, artifacts: _update_task(
            task_id, state="TASK_STATE_COMPLETED", message=result or "", artifacts=artifacts or []
        ),
        fail_fn=lambda error: _update_task(
            task_id, state="TASK_STATE_FAILED", message=str(error), artifacts=[]
        ),
        input_required_fn=lambda question, ctx: _update_task(
            task_id, state="TASK_STATE_INPUT_REQUIRED", message=question
        ),
    )
    _udt.configure_ui_provider_tools(
        message=message,
        permission_fn=lambda action, target: _require_ui_permission(
            action=action, target=target, message=message
        ),
        audit_fn=lambda operation, target, result, duration_ms=0: _write_ui_design_audit(
            workspace_path=workspace_path,
            message=message,
            operation=operation,
            target=target,
            result=result,
            duration_ms=duration_ms,
        ),
    )
    try:
        text = extract_text(message)
        system_prompt = build_system_prompt_from_manifest(os.path.dirname(__file__))
        require_agentic_runtime("UI-Design Agent")
        if workspace_path:
            record_workspace_stage(
                workspace_path,
                "ui-design",
                f"Started {capability or 'ui-design request'}",
                task_id=task_id,
                extra={"runtimeConfig": _runtime_config_summary()},
            )
        get_runtime().run_agentic(
            task=text,
            system_prompt=system_prompt,
            cwd=workspace_path or os.getcwd(),
            tools=[
                "figma_list_pages", "figma_fetch_page", "figma_fetch_node",
                "stitch_list_screens", "stitch_fetch_screen",
                "stitch_find_screen_by_name", "stitch_fetch_image",
                "report_progress", "complete_current_task", "fail_current_task",
                "load_skill",
            ],
            max_turns=15,
            timeout=300,
        )
        # Read final task state (set by complete_current_task / fail_current_task).
        # If neither tool was called (runtime loop exited cleanly), default to COMPLETED.
        with _TASK_SEQ_LOCK:
            final = _TASKS.get(task_id, {})
            final_state = (final.get("status") or {}).get("state", "TASK_STATE_WORKING")
            if final_state == "TASK_STATE_WORKING":
                # Runtime finished without calling complete/fail — treat as completed
                final_state = "TASK_STATE_COMPLETED"
                if final:
                    final["status"]["state"] = final_state
            final_artifacts = final.get("artifacts", [])
            final_msg_parts = ((final.get("status") or {}).get("message") or {}).get("parts") or []
            status_text = (final_msg_parts[0].get("text") if final_msg_parts else None) or "UI design operation completed."
        if workspace_path:
            record_workspace_stage(
                workspace_path,
                "ui-design",
                f"Completed {capability or 'ui-design request'}",
                task_id=task_id,
                extra={"statusText": status_text, "runtimeConfig": _runtime_config_summary()},
            )
        callback_url = metadata.get("orchestratorCallbackUrl", "")
        if callback_url:
            _fire_callback(callback_url, task_id, final_state, status_text, final_artifacts)
    except Exception as exc:
        print(f"[{AGENT_ID}] Task {task_id} failed: {exc}", flush=True)
        artifacts = []
        if isinstance(exc, PermissionDeniedError):
            artifacts = [build_permission_denied_artifact(exc.details, agent_id=AGENT_ID)]
        _update_task(task_id, state="TASK_STATE_FAILED", message=str(exc), artifacts=artifacts)
        if workspace_path:
            record_workspace_stage(
                workspace_path,
                "ui-design",
                f"Failed {capability or 'ui-design request'}",
                task_id=task_id,
                extra={"error": str(exc), "runtimeConfig": _runtime_config_summary()},
            )
        callback_url = metadata.get("orchestratorCallbackUrl", "")
        if callback_url:
            _fire_callback(callback_url, task_id, "TASK_STATE_FAILED", str(exc), artifacts)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class UIDesignHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if any(p in line for p in ("/health", "/.well-known/agent-card.json")):
            return
        print(
            f"[{AGENT_ID}] {line} "
            f"{args[1] if len(args) > 1 else ''} "
            f"{args[2] if len(args) > 2 else ''}"
        )

    def _send_json(self, code: int, payload: dict) -> None:
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

    # -----------------------------------------------------------------------
    # GET routes
    # -----------------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": AGENT_ID})
            return

        if path == "/.well-known/agent-card.json":
            self._send_json(200, _load_agent_card())
            return

        # --- Figma REST ---

        if path == "/figma/meta":
            figma_url = (qs.get("url") or [""])[0]
            if not figma_url:
                self._send_json(400, {"error": "missing url parameter"})
                return
            if not _enforce_http_ui_permission(self, action="figma.read", target=figma_url):
                return
            file_key, _ = figma_client.parse_figma_url(figma_url)
            if not file_key:
                self._send_json(400, {"error": "could_not_parse_figma_url"})
                return
            meta, status = figma_client.fetch_file_meta_cached(file_key)
            self._send_json(
                200 if status == "ok" else 502,
                {"fileKey": file_key, "status": status, "meta": meta},
            )
            return

        if path == "/figma/pages":
            figma_url = (qs.get("url") or [""])[0]
            if not figma_url:
                self._send_json(400, {"error": "missing url parameter"})
                return
            if not _enforce_http_ui_permission(self, action="figma.read", target=figma_url):
                return
            file_key, _ = figma_client.parse_figma_url(figma_url)
            if not file_key:
                self._send_json(400, {"error": "could_not_parse_figma_url"})
                return
            pages, status = figma_client.fetch_pages_cached(file_key)
            self._send_json(
                200 if status == "ok" else 502,
                {"fileKey": file_key, "status": status, "pages": pages},
            )
            return

        if path == "/figma/page":
            figma_url = (qs.get("url") or [""])[0]
            page_name = (qs.get("name") or [""])[0]
            if not figma_url or not page_name:
                self._send_json(400, {"error": "missing url or name parameter"})
                return
            if not _enforce_http_ui_permission(self, action="figma.read", target=figma_url):
                return
            file_key, _ = figma_client.parse_figma_url(figma_url)
            if not file_key:
                self._send_json(400, {"error": "could_not_parse_figma_url"})
                return
            result, status = figma_client.fetch_page_by_name(file_key, page_name)
            code = (
                200 if status == "ok"
                else 404 if status == "page_not_found"
                else 502
            )
            self._send_json(code, {"fileKey": file_key, "status": status, **result})
            return

        if path == "/figma/node":
            # Fetch design spec for a specific element/node.
            # Accept either: ?url=<figma_url> (node_id extracted from url)
            #            or: ?url=<figma_url>&node_id=<1:470>
            figma_url = (qs.get("url") or [""])[0]
            node_id = (qs.get("node_id") or [""])[0]
            if not figma_url:
                self._send_json(400, {"error": "missing url parameter"})
                return
            if not _enforce_http_ui_permission(self, action="element.inspect", target=figma_url):
                return
            file_key, url_node_id = figma_client.parse_figma_url(figma_url)
            if not file_key:
                self._send_json(400, {"error": "could_not_parse_figma_url"})
                return
            node_id = node_id or url_node_id or ""
            if not node_id:
                self._send_json(400, {"error": "node_id required (pass as ?node_id= or embed in Figma URL)"})
                return
            result, status = figma_client.fetch_nodes_cached(file_key, [node_id])
            self._send_json(
                200 if status == "ok" else 502,
                {"fileKey": file_key, "nodeId": node_id, "status": status, **result},
            )
            return

        # --- Stitch MCP ---

        if path == "/stitch/tools":
            if not _enforce_http_ui_permission(self, action="stitch.read", target="stitch/tools"):
                return
            tools, status = stitch_client.list_tools()
            self._send_json(
                200 if status == "ok" else 502,
                {"status": status, "tools": tools},
            )
            return

        if path == "/stitch/project":
            project_id = (qs.get("id") or [""])[0]
            if not project_id:
                self._send_json(400, {"error": "missing id parameter"})
                return
            if not _enforce_http_ui_permission(self, action="stitch.read", target=project_id):
                return
            result, status = stitch_client.get_project(project_id)
            self._send_json(
                200 if status == "ok" else 502,
                {"projectId": project_id, "status": status, **result},
            )
            return

        if path == "/stitch/screen":
            project_id = (qs.get("project_id") or [""])[0]
            screen_id = (qs.get("screen_id") or [""])[0]
            if not project_id or not screen_id:
                self._send_json(400, {"error": "missing project_id or screen_id"})
                return
            if not _enforce_http_ui_permission(self, action="stitch.read", target=f"{project_id}/{screen_id}"):
                return
            result, status = stitch_client.get_screen(project_id, screen_id)
            self._send_json(
                200 if status == "ok" else 502,
                {
                    "projectId": project_id,
                    "screenId": screen_id,
                    "status": status,
                    **result,
                },
            )
            return

        if path == "/stitch/screen/image":
            project_id = (qs.get("project_id") or [""])[0]
            screen_id = (qs.get("screen_id") or [""])[0]
            if not project_id or not screen_id:
                self._send_json(400, {"error": "missing project_id or screen_id"})
                return
            if not _enforce_http_ui_permission(self, action="stitch.read", target=f"{project_id}/{screen_id}/image"):
                return
            result, status = stitch_client.get_screen_image(project_id, screen_id)
            self._send_json(
                200 if status in ("ok", "tool_not_found") else 502,
                {
                    "projectId": project_id,
                    "screenId": screen_id,
                    "status": status,
                    **result,
                },
            )
            return

        # --- Task polling ---

        if path.startswith("/tasks/"):
            task_id = path[len("/tasks/"):]
            with _TASK_SEQ_LOCK:
                task = _TASKS.get(task_id)
            if task:
                # Return A2A-compatible task wrapper
                self._send_json(200, {
                    "task": {
                        "id": task.get("id", task_id),
                        "agentId": task.get("agentId", AGENT_ID),
                        "status": task.get("status", {"state": "TASK_STATE_WORKING"}),
                        "artifacts": task.get("artifacts", []),
                    }
                })
            else:
                self._send_json(404, {"error": "task_not_found"})
            return

        # --- Audit log query ---

        if path == "/audit":
            audit_qs = parse_qs(parsed.query)
            workspace = (audit_qs.get("workspace") or [""])[0]
            task_id_qs = (audit_qs.get("taskId") or [""])[0]
            operation = (audit_qs.get("operation") or [""])[0]
            if not workspace:
                self._send_json(400, {"error": "workspace parameter required"})
                return
            entries = self._read_ui_audit(workspace, task_id=task_id_qs, operation=operation)
            self._send_json(200, {"entries": entries, "count": len(entries)})
            return

        self._send_json(404, {"error": "not_found"})

    # -----------------------------------------------------------------------
    # POST routes
    # -----------------------------------------------------------------------

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/message:send":
            body = self._read_body()
            message = body.get("message", {})
            if not message:
                self._send_json(400, {"error": "missing message"})
                return

            task_id = _next_task_id()
            print(f"[{AGENT_ID}] Task {task_id} submitted", flush=True)

            _TASKS[task_id] = {
                "id": task_id,
                "agentId": AGENT_ID,
                "status": {
                    "state": "TASK_STATE_WORKING",
                    "message": {"role": "ROLE_AGENT", "parts": [{"text": "UI Design Agent processing the task."}]},
                },
                "artifacts": [],
            }

            threading.Thread(
                target=_run_task_background,
                args=(task_id, message),
                daemon=True,
            ).start()

            self._send_json(200, {"task": _TASKS[task_id]})
            return

        self._send_json(404, {"error": "not_found"})

    def _read_ui_audit(self, workspace: str, task_id: str = "", operation: str = "") -> list:
        """Read audit-log.jsonl from the given workspace directory."""
        audit_path = os.path.join(workspace, "ui-design-agent", "audit-log.jsonl")
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
                entries.append(entry)
        return entries


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    reporter = InstanceReporter(
        agent_id=AGENT_ID, service_url=ADVERTISED_URL, port=PORT
    )
    reporter.start()

    server = ThreadingHTTPServer((HOST, PORT), UIDesignHandler)
    print(f"[{AGENT_ID}] UI Design Agent starting on {HOST}:{PORT}", flush=True)
    print(f"[{AGENT_ID}] Advertised URL: {ADVERTISED_URL}", flush=True)
    try:
        server.serve_forever()
    finally:
        reporter.stop()


if __name__ == "__main__":
    main()
