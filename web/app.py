"""Web Agent -- full-stack web development execution agent.

Capabilities:
- Frontend: React, Next.js, Vue.js with Ant Design, Material UI, Tailwind CSS
- Backend: Python (Flask, FastAPI, Django), Node.js (Express, NestJS)
- Clones target repository, implements requested changes, validates locally
- Creates feature branch and pull request via SCM Agent
- Reports completion via A2A callback to Team Lead Agent

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

from common.agent_directory import AgentDirectory
from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.message_utils import build_text_artifact, extract_text
from common.orchestrator import resolve_orchestrator_base_url
from common.per_task_exit import PerTaskExitHandler
from common.prompt_builder import build_system_prompt_from_manifest
from common.registry_client import RegistryClient
from common.runtime.adapter import get_runtime, require_agentic_runtime
from common.task_permissions import (
    PermissionEscalationRequired,
    build_permission_denied_artifact,
)
from common.task_store import TaskStore
from common.time_utils import local_clock_time, local_iso_timestamp
from web.agentic_workflow import (
    WEB_AGENT_RUNTIME_TOOL_NAMES,
    build_web_agent_runtime_config,
    build_web_task_prompt,
    configure_web_agent_control_tools,
)

# ---------------------------------------------------------------------------
# Tool auto-registration -- import so tools self-register for run_agentic()
# ---------------------------------------------------------------------------
from common.tools import (  # noqa: F401 -- side-effect imports
    coding_tools,
    control_tools,
    design_tools,
    jira_tools,
    planning_tools,
    progress_tools,
    registry_tools,
    scm_tools,
    skill_tool,
    validation_tools,
)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8050"))
AGENT_ID = os.environ.get("AGENT_ID", "web-agent")
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{AGENT_ID}-local")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://web-agent:{PORT}")
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")

ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))
TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "1800"))
INPUT_WAIT_TIMEOUT = int(os.environ.get("INPUT_WAIT_TIMEOUT_SECONDS", "7200"))

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

# INPUT_REQUIRED pause/resume -- keyed by task_id
_INPUT_EVENTS: dict[str, dict] = {}
_INPUT_EVENTS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Callback helper
# ---------------------------------------------------------------------------

def _notify_callback(
    callback_url: str,
    task_id: str,
    state: str,
    status_message: str,
    artifacts: list | None = None,
) -> None:
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


# ---------------------------------------------------------------------------
# INPUT_REQUIRED pause / resume
# ---------------------------------------------------------------------------

def _make_wait_for_user_input(*, task_id: str, callback_url: str):
    """Return a blocking callable that pauses the runtime until the user replies."""

    def _wait(question: str) -> str | None:
        task_store.update_state(task_id, "TASK_STATE_INPUT_REQUIRED", question)
        event = threading.Event()
        with _INPUT_EVENTS_LOCK:
            _INPUT_EVENTS[task_id] = {"event": event, "info": None}
        _notify_callback(
            callback_url,
            task_id,
            "TASK_STATE_INPUT_REQUIRED",
            f"Web Agent needs input: {question}",
        )
        if not event.wait(timeout=INPUT_WAIT_TIMEOUT):
            with _INPUT_EVENTS_LOCK:
                _INPUT_EVENTS.pop(task_id, None)
            return None
        with _INPUT_EVENTS_LOCK:
            entry = _INPUT_EVENTS.pop(task_id, {})
        user_reply = entry.get("info") or ""
        task_store.update_state(task_id, "TASK_STATE_WORKING", "Resumed with user input")
        return user_reply

    return _wait


# ---------------------------------------------------------------------------
# Task exit rule
# ---------------------------------------------------------------------------

def _apply_task_exit_rule(task_id: str, exit_rule: dict) -> None:
    def _run() -> None:
        exit_handler.apply(
            task_id,
            exit_rule or {},
            shutdown_fn=_schedule_shutdown,
            agent_id=AGENT_ID,
        )

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _resolve_jira_context(user_text: str, metadata: dict) -> tuple:
    """Extract ticket key and Jira content snippet from handed-off metadata."""
    jira_ctx = metadata.get("jiraContext")
    if not isinstance(jira_ctx, dict):
        jira_ctx = {}

    ticket_key = str(jira_ctx.get("ticketKey") or "").strip()
    if not ticket_key:
        ticket_key = str(metadata.get("jiraTicketKey") or "").strip()
    if not ticket_key:
        m = re.search(r"\b([A-Z][A-Z0-9]+-\d{2,})\b", user_text or "")
        ticket_key = m.group(1) if m else ""

    content = str(jira_ctx.get("content") or "").strip()
    return ticket_key, content


def _prepend_tech_stack_constraints(task_instruction: str, constraints: dict) -> str:
    if not constraints:
        return task_instruction
    lines = ["HARD TECH STACK CONSTRAINTS:"]
    lang = constraints.get("language")
    if lang == "python":
        ver = constraints.get("python_version")
        lines.append("- Use Python" + ((" " + ver) if ver else "") + ".")
    if constraints.get("backend_framework"):
        lines.append("- Use " + constraints["backend_framework"] + " for the backend/web server.")
    if constraints.get("frontend_framework"):
        lines.append("- Use " + constraints["frontend_framework"] + " for the frontend.")
    lines.append("- Do not switch to React, Next.js, or Node.js unless the user explicitly overrides.")
    if lang == "python":
        lines.append("- If the target repo is empty or sparse, scaffold the required stack in-place.")
    block = "\n".join(lines)
    if block in task_instruction:
        return task_instruction
    return block + "\n\n" + task_instruction


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def _run_workflow(task_id: str, message: dict) -> None:
    """
    Agentic Web Agent workflow running in a background thread.

    The agentic runtime (connect-agent, copilot-cli, or claude-code) drives all
    workflow decisions via tools.  Python code only wires lifecycle callbacks,
    builds the initial task prompt, and handles I/O with Team Lead / Compass.
    """
    task = task_store.get(task_id)
    if not task:
        return

    metadata = message.get("metadata", {})
    compass_task_id = metadata.get("orchestratorTaskId", "")
    callback_url = metadata.get("orchestratorCallbackUrl", "")
    orchestrator_url = resolve_orchestrator_base_url(metadata, agent_directory=agent_directory)
    workspace = metadata.get("sharedWorkspacePath", "")
    permissions = (
        metadata.get("permissions")
        if isinstance(metadata.get("permissions"), dict)
        else None
    )
    acceptance_criteria = list(metadata.get("acceptanceCriteria") or [])
    is_revision = bool(metadata.get("isRevision", False))
    review_issues = list(metadata.get("reviewIssues") or [])
    tech_stack_constraints = dict(metadata.get("techStackConstraints") or {})
    design_context_meta = dict(metadata.get("designContext") or {})
    target_repo_url = str(metadata.get("targetRepoUrl", ""))
    exit_rule = PerTaskExitHandler.parse(metadata)

    user_text = _prepend_tech_stack_constraints(
        extract_text(message) or "", tech_stack_constraints
    )
    if is_revision and review_issues:
        issues_text = "\n".join("- " + issue for issue in review_issues)
        user_text = user_text + "\n\nREVISION REQUEST -- please fix the following issues:\n" + issues_text

    ticket_key, jira_content = _resolve_jira_context(user_text, metadata)

    runtime_config = build_web_agent_runtime_config()
    wait_for_input = _make_wait_for_user_input(task_id=task_id, callback_url=callback_url)
    configure_web_agent_control_tools(
        task_id=task_id,
        agent_id=AGENT_ID,
        workspace=workspace,
        permissions=permissions,
        compass_task_id=compass_task_id,
        callback_url=callback_url,
        orchestrator_url=orchestrator_url,
        user_text=user_text,
        wait_for_input_fn=wait_for_input,
    )

    def log(phase: str) -> None:
        ts = local_clock_time()
        entry = "[" + ts + "] " + phase
        print("[" + AGENT_ID + "][" + task_id + "] " + phase)
        _append_workspace_file(workspace, AGENT_ID + "/command-log.txt", entry + "\n")

    task_store.update_state(task_id, "TASK_STATE_WORKING", "Web Agent is starting.")
    audit_log("TASK_STARTED", task_id=task_id, compass_task_id=compass_task_id)

    try:
        task_prompt = build_web_task_prompt(
            user_text=user_text,
            workspace=workspace,
            compass_task_id=compass_task_id,
            web_task_id=task_id,
            acceptance_criteria=acceptance_criteria,
            is_revision=is_revision,
            review_issues=review_issues,
            tech_stack_constraints=tech_stack_constraints,
            design_context=design_context_meta,
            target_repo_url=target_repo_url,
            jira_context=jira_content,
            ticket_key=ticket_key or "",
            permissions=permissions,
        )
    except RuntimeError as err:
        error_text = str(err)
        print("[" + AGENT_ID + "][" + task_id + "] Prompt build failed: " + error_text)
        task_store.update_state(task_id, "TASK_STATE_FAILED", error_text)
        _notify_callback(callback_url, task_id, "TASK_STATE_FAILED", error_text)
        _apply_task_exit_rule(task_id, exit_rule)
        return

    system_prompt = build_system_prompt_from_manifest(
        os.path.dirname(os.path.abspath(__file__))
    )
    require_agentic_runtime("Web Agent")
    runtime = get_runtime()

    log("Starting agentic workflow")

    try:
        result = runtime.run_agentic(
            task=task_prompt,
            system_prompt=system_prompt,
            cwd=workspace or os.getcwd(),
            tools=WEB_AGENT_RUNTIME_TOOL_NAMES,
            max_turns=80,
            timeout=TASK_TIMEOUT,
        )

        summary = result.summary or "Web Agent task completed."
        final_artifacts = [
            build_text_artifact(
                "web-agent-summary",
                summary,
                metadata={
                    "agentId": AGENT_ID,
                    "capability": "web.task.execute",
                    "orchestratorTaskId": compass_task_id,
                    "taskId": task_id,
                },
            )
        ]
        if result.artifacts:
            final_artifacts.extend(result.artifacts)

        if result.success:
            task_store.update_state(task_id, "TASK_STATE_COMPLETED", summary)
            task = task_store.get(task_id)
            if task:
                task.artifacts = final_artifacts
            log("Task completed successfully")
            audit_log("TASK_COMPLETED", task_id=task_id, compass_task_id=compass_task_id)
        else:
            task_store.update_state(task_id, "TASK_STATE_FAILED", summary)
            task = task_store.get(task_id)
            if task:
                task.artifacts = final_artifacts
            log("Task failed: " + summary[:200])
            audit_log(
                "TASK_FAILED",
                task_id=task_id,
                compass_task_id=compass_task_id,
                error=summary[:300],
            )

        _save_workspace_file(
            workspace,
            AGENT_ID + "/stage-summary.json",
            json.dumps(
                {
                    "taskId": task_id,
                    "agentId": AGENT_ID,
                    "currentPhase": "COMPLETED" if result.success else "FAILED",
                    "runtimeConfig": runtime_config,
                    "turnsUsed": result.turns_used,
                    "updatedAt": local_iso_timestamp(),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

        _notify_callback(
            callback_url,
            task_id,
            "TASK_STATE_COMPLETED" if result.success else "TASK_STATE_FAILED",
            summary,
            final_artifacts,
        )

    except Exception as err:
        error_text = str(err)
        failure_artifacts = []
        if isinstance(err, PermissionEscalationRequired):
            failure_artifacts = [build_permission_denied_artifact(err.details, agent_id=AGENT_ID)]
        print("[" + AGENT_ID + "][" + task_id + "] FAILED: " + error_text)
        task_store.update_state(
            task_id, "TASK_STATE_FAILED", "Web Agent failed: " + error_text[:500]
        )
        task = task_store.get(task_id)
        if task:
            task.artifacts = failure_artifacts
        audit_log("TASK_FAILED", task_id=task_id, error=error_text[:300])
        _notify_callback(
            callback_url,
            task_id,
            "TASK_STATE_FAILED",
            "Web Agent failed: " + error_text[:500],
            failure_artifacts,
        )

    finally:
        _apply_task_exit_rule(task_id, exit_rule)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class WebAgentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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

    def do_GET(self) -> None:
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

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        # ACK endpoint -- parent confirms it received the callback
        m_ack = re.fullmatch(r"/tasks/([^/]+)/ack", path)
        if m_ack:
            task_id = m_ack.group(1)
            acked = exit_handler.acknowledge(task_id)
            print("[" + AGENT_ID + "] Received ACK for task " + task_id + " (registered=" + str(acked) + ")")
            self._send_json(200, {"ok": True, "task_id": task_id})
            return

        # Resume endpoint -- forwarded user reply for INPUT_REQUIRED tasks
        m_resume = re.fullmatch(r"/tasks/([^/]+)/resume", path)
        if m_resume:
            task_id = m_resume.group(1)
            body = self._read_body()
            user_reply = str(body.get("reply") or body.get("message") or "").strip()
            with _INPUT_EVENTS_LOCK:
                entry = _INPUT_EVENTS.get(task_id)
            if entry:
                entry["info"] = user_reply
                entry["event"].set()
                self._send_json(200, {"ok": True, "task_id": task_id})
            else:
                self._send_json(404, {"error": "task_not_waiting_for_input"})
            return

        if path != "/message:send":
            self._send_json(404, {"error": "not_found"})
            return

        body = self._read_body()
        message = body.get("message", {})
        if not message:
            self._send_json(400, {"error": "missing_message"})
            return

        # Resume an INPUT_REQUIRED task when contextId is set
        context_id = str((message.get("metadata") or {}).get("contextId") or "").strip()
        if context_id:
            user_reply = extract_text(message) or ""
            with _INPUT_EVENTS_LOCK:
                entry = _INPUT_EVENTS.get(context_id)
            if entry:
                entry["info"] = user_reply
                entry["event"].set()
                task = task_store.get(context_id)
                self._send_json(200, {"task": task.to_dict() if task else {"id": context_id}})
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

    def log_message(self, fmt, *args) -> None:
        line = args[0] if args else ""
        if any(p in line for p in ("/health", "/.well-known/agent-card.json")):
            return
        a1 = args[1] if len(args) > 1 else ""
        a2 = args[2] if len(args) > 2 else ""
        print("[" + AGENT_ID + "] " + line + " " + str(a1) + " " + str(a2))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_SERVER = None


def _schedule_shutdown(delay_seconds: int = 5) -> None:
    def _do_shutdown() -> None:
        time.sleep(delay_seconds)
        print("[" + AGENT_ID + "] Per-task shutdown triggered")
        if _SERVER:
            _SERVER.shutdown()

    threading.Thread(target=_do_shutdown, daemon=True).start()


def main() -> None:
    global _SERVER
    print("[" + AGENT_ID + "] Web Agent starting on " + HOST + ":" + str(PORT))
    agent_directory.start()
    _SERVER = ThreadingHTTPServer((HOST, PORT), WebAgentHandler)
    reporter.start()
    _SERVER.serve_forever()


if __name__ == "__main__":
    main()
