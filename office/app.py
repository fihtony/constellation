"""Office Agent — local document summary, analysis, and organize execution agent.

Architecture:
  All workflow decisions are made by the agentic runtime via tools.
  Python code handles only: A2A protocol, task lifecycle, tool wiring, and I/O.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.message_utils import extract_text
from office.agentic_workflow import (
    OFFICE_AGENT_RUNTIME_TOOL_NAMES,
    build_office_agent_runtime_config,
    build_office_task_prompt,
    configure_office_control_tools,
)
from common.orchestrator import resolve_orchestrator_base_url
from common.per_task_exit import PerTaskExitHandler
from common.prompt_builder import build_system_prompt_from_manifest
from common.runtime.adapter import get_runtime
from common.task_permissions import (
    PermissionDeniedError,
    audit_permission_check,
    build_permission_denied_artifact,
    build_permission_denied_details,
    parse_permission_grant,
)
from common.task_store import TaskStore
from common.time_utils import local_clock_time, local_iso_timestamp

# Tool auto-registration — import so tools self-register for run_agentic()
from common.tools import (  # noqa: F401 -- side-effect imports
    coding_tools,
    control_tools,
    planning_tools,
    progress_tools,
    registry_tools,
    skill_tool,
    validation_tools,
)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8060"))
AGENT_ID = os.environ.get("AGENT_ID", "office-agent")
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{AGENT_ID}-local")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://office-agent:{PORT}")

TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "1200"))
INPUT_ROOT = "/app/userdata"

_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))

task_store = TaskStore()
exit_handler = PerTaskExitHandler()
reporter = InstanceReporter(
    agent_id=AGENT_ID,
    service_url=ADVERTISED_URL,
    port=PORT,
)

_SERVER: ThreadingHTTPServer | None = None


def audit_log(event: str, **kwargs) -> None:
    entry = {"ts": local_iso_timestamp(), "event": event, **kwargs}
    print(f"[audit] {json.dumps(entry, ensure_ascii=False)}")


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------


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


def _permission_enforcement_mode() -> str:
    return os.environ.get("PERMISSION_ENFORCEMENT", "strict").strip().lower() or "strict"


def _check_office_permission(
    *,
    action: str,
    target: str,
    metadata: dict,
    scope: str = "*",
) -> tuple[bool, str, str]:
    if _permission_enforcement_mode() == "off":
        return True, "allowed", ""

    request_agent = str(metadata.get("requestAgent") or "compass-agent").strip() or "compass-agent"
    task_id = str(metadata.get("orchestratorTaskId") or metadata.get("taskId") or "").strip()
    permissions_data = metadata.get("permissions") if isinstance(metadata.get("permissions"), dict) else None
    grant = parse_permission_grant(permissions_data)
    if grant:
        allowed, reason = grant.check("office", action, scope)
        escalation = grant.escalation_for("office", action, scope)
    else:
        allowed = False
        reason = "No permissions attached to request. Explicit permission grant required."
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


def _require_office_permission(
    *,
    action: str,
    target: str,
    metadata: dict,
    scope: str = "*",
) -> None:
    allowed, reason, escalation = _check_office_permission(
        action=action,
        target=target,
        metadata=metadata,
        scope=scope,
    )
    if allowed:
        return
    if _permission_enforcement_mode() == "strict":
        raise PermissionDeniedError(
            build_permission_denied_details(
                permission_agent="office",
                target_agent=AGENT_ID,
                action=action,
                target=target,
                reason=reason,
                escalation=escalation or "require_user_approval",
                scope=scope,
                request_agent=str(metadata.get("requestAgent") or "compass-agent").strip() or "compass-agent",
                task_id=str(metadata.get("taskId") or ""),
                orchestrator_task_id=str(metadata.get("orchestratorTaskId") or ""),
            )
        )

    print(f"[{AGENT_ID}] WARN: permission check failed but enforcement={_permission_enforcement_mode()}: {reason}")


def _enforce_office_task_permissions(metadata: dict, target_paths: list[str], input_root: str, output_mode: str) -> None:
    for path in target_paths:
        if not _path_within_base(path, input_root):
            _require_office_permission(
                action="access_outside_root",
                target=path,
                metadata=metadata,
                scope="*",
            )
            raise RuntimeError(f"Target path escapes mounted input root: {path}")

    root_target = os.path.commonpath(target_paths)
    _require_office_permission(
        action="read",
        target=root_target,
        metadata=metadata,
        scope="task_root",
    )
    if output_mode == "inplace":
        _require_office_permission(
            action="write",
            target=root_target,
            metadata=metadata,
            scope="task_root",
        )


def _path_within_base(path: str, base: str) -> bool:
    try:
        common = os.path.commonpath([os.path.realpath(path), os.path.realpath(base)])
    except ValueError:
        return False
    return common == os.path.realpath(base)


def _notify_callback(callback_url: str, task_id: str, state: str, status_message: str, artifacts: list | None = None):
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
    except Exception as err:
        print(f"[{AGENT_ID}] Callback failed: {err}")


def _report_progress(compass_url: str, compass_task_id: str, step: str) -> None:
    if not compass_url or not compass_task_id or not step:
        return
    payload = {"step": step, "agentId": AGENT_ID}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{compass_url.rstrip('/')}/tasks/{compass_task_id}/progress",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5):
            pass
    except Exception as err:
        print(f"[{AGENT_ID}] Progress report failed (non-critical): {err}")


def _schedule_shutdown(delay_seconds: int = 5) -> None:
    def _shutdown():
        time.sleep(delay_seconds)
        print(f"[{AGENT_ID}] Per-task shutdown triggered")
        if _SERVER:
            _SERVER.shutdown()

    threading.Thread(target=_shutdown, daemon=True).start()


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
            {**(exit_rule or {}), "type": rule_type},
            shutdown_fn=_schedule_shutdown,
            agent_id=AGENT_ID,
        )

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------


def _run_workflow(task_id: str, message: dict) -> None:
    """
    Office Agent workflow running in a background thread.

    The agentic runtime (connect-agent, copilot-cli, or claude-code) drives all
    workflow decisions via tools. Python code only wires lifecycle callbacks,
    builds the initial task prompt, and handles I/O with Compass.
    """
    metadata = dict(message.get("metadata") or {})
    callback_url = str(metadata.get("orchestratorCallbackUrl") or "")
    compass_url = resolve_orchestrator_base_url(metadata)
    compass_task_id = str(metadata.get("orchestratorTaskId") or "")
    exit_rule = PerTaskExitHandler.parse(metadata)
    workspace_path = str(metadata.get("sharedWorkspacePath") or "")
    task = task_store.get(task_id)
    if not task:
        return

    capability = str(metadata.get("requestedCapability") or "").strip()
    user_text = extract_text(message)
    input_root = str(metadata.get("officeInputRoot") or INPUT_ROOT).strip() or INPUT_ROOT
    output_mode = str(metadata.get("officeOutputMode") or "workspace")
    target_paths = [os.path.realpath(p) for p in (metadata.get("officeTargetPaths") or []) if str(p).strip()]

    runtime_config = build_office_agent_runtime_config()

    def log(phase: str) -> None:
        ts = local_clock_time()
        entry = f"[{ts}] {phase}"
        print(f"[{AGENT_ID}][{task_id}] {phase}")
        _append_workspace_file(workspace_path, "office-agent/command-log.txt", entry + "\n")
        _report_progress(compass_url, compass_task_id, phase)

    # Validate permissions before starting agentic execution
    if target_paths:
        try:
            _enforce_office_task_permissions(metadata, target_paths, input_root, output_mode)
        except (PermissionDeniedError, RuntimeError) as perm_err:
            failure = f"Office task failed: {perm_err}"
            artifacts = []
            if isinstance(perm_err, PermissionDeniedError):
                artifacts = [build_permission_denied_artifact(perm_err.details, agent_id=AGENT_ID)]
            task_store.update_state(task_id, "TASK_STATE_FAILED", failure)
            task.artifacts = artifacts
            _notify_callback(callback_url, task_id, "TASK_STATE_FAILED", failure, artifacts)
            audit_log("TASK_FAILED", task_id=task_id, error=str(perm_err))
            _apply_task_exit_rule(task_id, exit_rule)
            return

    configure_office_control_tools(
        task_id=task_id,
        agent_id=AGENT_ID,
        workspace=workspace_path,
        permissions=metadata.get("permissions"),
        compass_task_id=compass_task_id,
        callback_url=callback_url,
        orchestrator_url=compass_url,
        user_text=user_text,
    )

    task_store.update_state(task_id, "TASK_STATE_WORKING", "Office Agent is processing the request.")
    log("Office Agent starting")
    audit_log("TASK_STARTED", task_id=task_id, capability=capability)

    system_prompt = build_system_prompt_from_manifest(_AGENT_DIR)
    task_prompt = build_office_task_prompt(
        user_text=user_text,
        capability=capability,
        target_paths=target_paths,
        output_mode=output_mode,
        workspace_path=workspace_path,
        task_id=task_id,
        compass_task_id=compass_task_id,
        agent_dir=_AGENT_DIR,
    )

    cwd = target_paths[0] if target_paths else workspace_path or os.getcwd()

    try:
        log("Starting agentic workflow")
        runtime = get_runtime()
        result = runtime.run_agentic(
            task=task_prompt,
            system_prompt=system_prompt,
            cwd=cwd,
            tools=OFFICE_AGENT_RUNTIME_TOOL_NAMES,
            max_turns=40,
            timeout=TASK_TIMEOUT,
        )

        summary = result.summary or "Office task completed."
        final_artifacts: list = list(result.artifacts or [])

        summary_artifact = {
            "name": "office-agent-summary",
            "artifactType": "text/plain",
            "parts": [{"text": summary}],
            "metadata": {
                "agentId": AGENT_ID,
                "capability": capability,
                "orchestratorTaskId": compass_task_id,
                "taskId": task_id,
            },
        }
        final_artifacts.insert(0, summary_artifact)

        if result.success:
            task_store.update_state(task_id, "TASK_STATE_COMPLETED", summary)
            task.artifacts = final_artifacts
            log(f"Task completed: {summary[:100]}")
            _save_workspace_file(
                workspace_path,
                "office-agent/stage-summary.json",
                json.dumps(
                    {
                        "taskId": task_id,
                        "agentId": AGENT_ID,
                        "currentPhase": "COMPLETED",
                        "runtimeConfig": runtime_config,
                        "turnsUsed": getattr(result, "turns_used", None),
                        "updatedAt": local_iso_timestamp(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            _notify_callback(callback_url, task_id, "TASK_STATE_COMPLETED", summary, final_artifacts)
            audit_log("TASK_COMPLETED", task_id=task_id, capability=capability)
        else:
            task_store.update_state(task_id, "TASK_STATE_FAILED", summary)
            task.artifacts = final_artifacts
            log(f"Task failed: {summary[:100]}")
            _save_workspace_file(
                workspace_path,
                "office-agent/stage-summary.json",
                json.dumps(
                    {
                        "taskId": task_id,
                        "agentId": AGENT_ID,
                        "currentPhase": "FAILED",
                        "error": summary,
                        "runtimeConfig": runtime_config,
                        "turnsUsed": getattr(result, "turns_used", None),
                        "updatedAt": local_iso_timestamp(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            _notify_callback(callback_url, task_id, "TASK_STATE_FAILED", summary, final_artifacts)
            audit_log("TASK_FAILED", task_id=task_id, error=summary[:300])

    except Exception as err:
        failure = f"Office task failed: {err}"
        artifacts = []
        if isinstance(err, PermissionDeniedError):
            artifacts = [build_permission_denied_artifact(err.details, agent_id=AGENT_ID)]
        task_store.update_state(task_id, "TASK_STATE_FAILED", failure)
        task.artifacts = artifacts
        log(f"FAILED: {str(err)[:200]}")
        _save_workspace_file(workspace_path, "office-agent/failure.txt", failure + "\n")
        _save_workspace_file(
            workspace_path,
            "office-agent/stage-summary.json",
            json.dumps(
                {
                    "taskId": task_id,
                    "agentId": AGENT_ID,
                    "currentPhase": "FAILED",
                    "error": str(err)[:500],
                    "runtimeConfig": runtime_config,
                    "updatedAt": local_iso_timestamp(),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        _notify_callback(callback_url, task_id, "TASK_STATE_FAILED", failure, artifacts)
        audit_log("TASK_FAILED", task_id=task_id, error=str(err))

    finally:
        _apply_task_exit_rule(task_id, exit_rule)


class OfficeAgentHandler(BaseHTTPRequestHandler):
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
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok", "service": AGENT_ID})
            return
        if path == "/.well-known/agent-card.json":
            card_path = os.path.join(os.path.dirname(__file__), "agent-card.json")
            with open(card_path, encoding="utf-8") as handle:
                card = json.load(handle)
            text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
            self._send_json(200, json.loads(text))
            return
        match = re.fullmatch(r"/tasks/([^/]+)", path)
        if match:
            task = task_store.get(match.group(1))
            if not task:
                self._send_json(404, {"error": "task_not_found"})
                return
            self._send_json(200, {"task": task.to_dict()})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        match = re.fullmatch(r"/tasks/([^/]+)/ack", path)
        if match:
            task_id = match.group(1)
            acked = exit_handler.acknowledge(task_id)
            print(f"[{AGENT_ID}] Received ACK for task {task_id} (registered={acked})")
            self._send_json(200, {"ok": True, "task_id": task_id})
            return

        if path != "/message:send":
            self._send_json(404, {"error": "not_found"})
            return

        body = self._read_body()
        message = body.get("message") or {}
        if not message:
            self._send_json(400, {"error": "missing_message"})
            return

        task = task_store.create()
        audit_log(
            "TASK_RECEIVED",
            task_id=task.task_id,
            capability=(message.get("metadata") or {}).get("requestedCapability", ""),
            instruction_preview=extract_text(message)[:120],
        )
        worker = threading.Thread(target=_run_workflow, args=(task.task_id, message), daemon=True)
        worker.start()
        self._send_json(200, {"task": task.to_dict()})

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if any(part in line for part in ("/health", "/.well-known/agent-card.json")):
            return
        print(
            f"[{AGENT_ID}] {line} "
            f"{args[1] if len(args) > 1 else ''} "
            f"{args[2] if len(args) > 2 else ''}"
        )


def main():
    global _SERVER
    print(f"[{AGENT_ID}] Office Agent starting on {HOST}:{PORT}")
    reporter.start()
    _SERVER = ThreadingHTTPServer((HOST, PORT), OfficeAgentHandler)
    _SERVER.serve_forever()


if __name__ == "__main__":
    main()