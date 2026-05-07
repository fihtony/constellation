"""Team Lead Agent — analyzes tasks, coordinates sub-agents, reviews output.

Responsibilities:
- Analyze incoming tasks from Compass
- Gather context (Jira, SCM, Design) via registered boundary agents using tools
- Plan and dispatch work to development agents (android, ios, web)
- Review development agent output and request revisions if needed
- Report major progress steps to Compass
- Summarize and finalize the task with callback to Compass

LLM-driven: All workflow decisions are made by the agentic runtime via tools.
Python code handles only: protocol, task lifecycle, tool wiring, and I/O.
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
from common.launcher import get_launcher
from common.message_utils import build_text_artifact, extract_text
from common.orchestrator import resolve_orchestrator_base_url
from common.per_task_exit import PerTaskExitHandler
from common.registry_client import RegistryClient
from common.prompt_builder import build_system_prompt_from_manifest
from common.runtime.adapter import get_runtime, require_agentic_runtime
from common.task_store import TaskStore
from common.team_lead_agentic_workflow import (
    TEAM_LEAD_RUNTIME_TOOL_NAMES,
    TEAM_LEAD_INPUT_REQUIRED_PREAMBLE,
    build_team_lead_runtime_config,
    build_team_lead_task_prompt as _build_team_lead_task_prompt,
    configure_team_lead_control_tools,
    make_wait_for_user_input,
)
from common.time_utils import local_clock_time, local_iso_timestamp

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8030"))
AGENT_ID = os.environ.get("AGENT_ID", "team-lead-agent")
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{AGENT_ID}-local")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://team-lead:{PORT}")
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")

TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "3600"))
INPUT_WAIT_TIMEOUT = int(os.environ.get("INPUT_WAIT_TIMEOUT_SECONDS", "7200"))
MAX_REVIEW_CYCLES = int(os.environ.get("MAX_REVIEW_CYCLES", "2"))
COMPASS_ACK_TIMEOUT = int(os.environ.get("COMPASS_ACK_TIMEOUT_SECONDS", "300"))

_AGENT_CARD_PATH = os.path.join(os.path.dirname(__file__), "agent-card.json")

registry = RegistryClient(REGISTRY_URL)
agent_directory = AgentDirectory(AGENT_ID, registry)
launcher = get_launcher()
exit_handler = PerTaskExitHandler()
task_store = TaskStore()
reporter = InstanceReporter(
    agent_id=AGENT_ID,
    service_url=ADVERTISED_URL,
    port=PORT,
)

# Per-task internal workflow context (not exposed externally)
_TASK_CONTEXTS: dict[str, "_TaskContext"] = {}
_TASK_CONTEXTS_LOCK = threading.Lock()

# Events for INPUT_REQUIRED -> resume flow
_INPUT_EVENTS: dict[str, dict] = {}  # task_id -> {"event": Event, "info": str | None}
_INPUT_EVENTS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Task context
# ---------------------------------------------------------------------------

class _TaskContext:
    """Minimal per-task state for the Team Lead workflow."""

    __slots__ = (
        "compass_task_id",
        "compass_callback_url",
        "compass_url",
        "shared_workspace_path",
        "permissions",
        "original_message",
        "user_text",
        "phases_log",
    )

    def __init__(self):
        self.compass_task_id: str = ""
        self.compass_callback_url: str = ""
        self.compass_url: str = ""
        self.shared_workspace_path: str = ""
        self.permissions: dict | None = None
        self.original_message: dict = {}
        self.user_text: str = ""
        self.phases_log: list[str] = []


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------

def _save_workspace_file(workspace_path: str, relative_name: str, content: str) -> None:
    """Write content to a file inside the shared workspace (best-effort)."""
    if not workspace_path:
        return
    try:
        full_path = os.path.join(workspace_path, relative_name)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"[{AGENT_ID}] Saved workspace file: {relative_name}")
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


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def audit_log(event: str, **kwargs):
    entry = {"ts": local_iso_timestamp(), "event": event, **kwargs}
    print(f"[audit] {json.dumps(entry, ensure_ascii=False)}")


# ---------------------------------------------------------------------------
# Progress / Callback helpers
# ---------------------------------------------------------------------------

def _report_progress(orchestrator_url: str, compass_task_id: str, step: str):
    """POST a progress step to the orchestrator (best-effort, non-critical)."""
    if not orchestrator_url or not compass_task_id:
        return
    payload = {"step": step, "agentId": AGENT_ID}
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{orchestrator_url.rstrip('/')}/tasks/{compass_task_id}/progress",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5):
            pass
    except Exception as err:
        print(f"[{AGENT_ID}] Progress report failed (non-critical): {err}")


def _notify_compass(
    callback_url: str,
    team_lead_task_id: str,
    state: str,
    status_message: str,
    artifacts: list | None = None,
):
    """Notify Compass of task completion or status change via callback URL."""
    if not callback_url:
        return
    payload = {
        "downstreamTaskId": team_lead_task_id,
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
        print(f"[{AGENT_ID}] Compass notified: task={team_lead_task_id} state={state}")
    except Exception as err:
        print(f"[{AGENT_ID}] Compass callback failed: {err}")


# ---------------------------------------------------------------------------
# Failure summary helper (deterministic fallback only)
# ---------------------------------------------------------------------------

def _build_failure_summary(user_text: str, phases_log: list[str], error_text: str) -> str:
    request_excerpt = " ".join((user_text or "").split())[:180] or "(request unavailable)"
    phase_excerpt = " | ".join((phases_log or [])[-5:]) or "no recorded phases"
    return (
        f"Task failed while handling request: {request_excerpt}. "
        f"Recent phases: {phase_excerpt}. "
        f"Error: {error_text[:300]}"
    )


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def _run_workflow(team_lead_task_id: str, ctx: _TaskContext):
    """
    Full Team Lead workflow running in a background thread.

    The agentic runtime (connect-agent, copilot-cli, or claude-code) drives all
    workflow decisions via tools. Python code only wires lifecycle callbacks,
    builds the initial task prompt, and handles I/O with Compass.
    """
    task = task_store.get(team_lead_task_id)
    if not task:
        return

    orchestrator_url = ctx.compass_url
    compass_task_id = ctx.compass_task_id
    callback_url = ctx.compass_callback_url
    workspace = ctx.shared_workspace_path
    user_text = ctx.user_text
    runtime_config = build_team_lead_runtime_config()
    wait_for_user_input = make_wait_for_user_input(
        task_id=team_lead_task_id,
        callback_url=callback_url,
        task_store=task_store,
        input_events=_INPUT_EVENTS,
        input_events_lock=_INPUT_EVENTS_LOCK,
        notify_compass=_notify_compass,
        input_wait_timeout=INPUT_WAIT_TIMEOUT,
        input_required_preamble=TEAM_LEAD_INPUT_REQUIRED_PREAMBLE,
    )
    configure_team_lead_control_tools(
        task_id=team_lead_task_id,
        agent_id=AGENT_ID,
        workspace=workspace,
        permissions=ctx.permissions,
        compass_task_id=compass_task_id,
        callback_url=callback_url,
        orchestrator_url=orchestrator_url,
        user_text=user_text,
        wait_for_input_fn=wait_for_user_input,
    )

    def log(phase: str):
        ts = local_clock_time()
        entry = f"[{ts}] {phase}"
        ctx.phases_log.append(entry)
        print(f"[{AGENT_ID}][{team_lead_task_id}] {phase}")
        _append_workspace_file(workspace, "team-lead/command-log.txt", entry + "\n")
        _report_progress(orchestrator_url, compass_task_id, phase)

    system_prompt = build_system_prompt_from_manifest(os.path.dirname(__file__))
    stop_before_dev_dispatch = bool(
        (ctx.original_message or {}).get("metadata", {}).get("stopBeforeDevDispatch", False)
    )
    task_prompt = _build_team_lead_task_prompt(
        user_text=user_text,
        workspace=workspace,
        compass_task_id=compass_task_id,
        team_lead_task_id=team_lead_task_id,
        callback_url=callback_url,
        max_review_cycles=MAX_REVIEW_CYCLES,
        stop_before_dev_dispatch=stop_before_dev_dispatch,
    )

    try:
        log("Starting agentic workflow")
        task_store.update_state(team_lead_task_id, "TASK_STATE_WORKING", "Analyzing task...")

        runtime = get_runtime()
        result = runtime.run_agentic(
            task=task_prompt,
            system_prompt=system_prompt,
            cwd=workspace or os.getcwd(),
            tools=TEAM_LEAD_RUNTIME_TOOL_NAMES,
            max_turns=100,
            timeout=TASK_TIMEOUT,
        )

        summary = result.summary or "Team Lead workflow completed."
        final_artifacts = [
            build_text_artifact(
                "team-lead-summary",
                summary,
                metadata={
                    "agentId": AGENT_ID,
                    "capability": "team-lead.task.analyze",
                    "orchestratorTaskId": compass_task_id,
                    "teamLeadTaskId": team_lead_task_id,
                },
            )
        ]
        if result.artifacts:
            final_artifacts.extend(result.artifacts)

        if result.success:
            task_store.update_state(team_lead_task_id, "TASK_STATE_COMPLETED", summary)
            log("Task completed successfully")
            audit_log("TASK_COMPLETED", task_id=team_lead_task_id, compass_task_id=compass_task_id)
        else:
            task_store.update_state(team_lead_task_id, "TASK_STATE_FAILED", summary)
            log(f"Task failed: {summary[:200]}")
            audit_log(
                "TASK_FAILED",
                task_id=team_lead_task_id,
                compass_task_id=compass_task_id,
                error=summary[:300],
            )

        _save_workspace_file(workspace, "team-lead/final-summary.md", summary)
        _save_workspace_file(
            workspace,
            "team-lead/stage-summary.json",
            json.dumps(
                {
                    "taskId": team_lead_task_id,
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

        exit_handler.register(team_lead_task_id)
        state = "TASK_STATE_COMPLETED" if result.success else "TASK_STATE_FAILED"
        _notify_compass(callback_url, team_lead_task_id, state, summary, final_artifacts)

    except Exception as err:
        error_text = str(err)
        print(f"[{AGENT_ID}][{team_lead_task_id}] FAILED: {error_text}")
        log(f"FAILED: {error_text[:300]}")

        failure_summary = _build_failure_summary(user_text, ctx.phases_log, error_text)

        task_store.update_state(team_lead_task_id, "TASK_STATE_FAILED", failure_summary)
        audit_log(
            "TASK_FAILED",
            task_id=team_lead_task_id,
            compass_task_id=compass_task_id,
            error=error_text[:300],
        )
        exit_handler.register(team_lead_task_id)
        _notify_compass(callback_url, team_lead_task_id, "TASK_STATE_FAILED", failure_summary)

    finally:
        def _delayed_cleanup():
            time.sleep(5)
            with _TASK_CONTEXTS_LOCK:
                _TASK_CONTEXTS.pop(team_lead_task_id, None)
            acked = exit_handler.wait(team_lead_task_id, timeout=COMPASS_ACK_TIMEOUT)
            if acked:
                print(f"[{AGENT_ID}] Compass ACK received for task {team_lead_task_id} — shutting down")
            else:
                print(
                    f"[{AGENT_ID}] Compass ACK timeout ({COMPASS_ACK_TIMEOUT}s) "
                    f"for task {team_lead_task_id} — shutting down"
                )
            _schedule_shutdown(delay_seconds=2)

        threading.Thread(target=_delayed_cleanup, daemon=True).start()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class TeamLeadHandler(BaseHTTPRequestHandler):
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

        # POST /tasks/{id}/ack — Compass confirms it received our callback
        m_ack = re.fullmatch(r"/tasks/([^/]+)/ack", path)
        if m_ack:
            task_id = m_ack.group(1)
            acked = exit_handler.acknowledge(task_id)
            print(f"[{AGENT_ID}] Received ACK from Compass for task {task_id} (registered={acked})")
            self._send_json(200, {"ok": True, "task_id": task_id})
            return

        # POST /tasks/{id}/callbacks — dev agent notifies completion (best-effort ack)
        # The agentic runtime uses wait_for_agent_task (polling) as the primary
        # completion mechanism; this endpoint provides early notification only.
        m_cb = re.fullmatch(r"/tasks/([^/]+)/callbacks", path)
        if m_cb:
            team_lead_task_id = m_cb.group(1)
            body = self._read_body()
            dev_task_id = (body.get("downstreamTaskId") or body.get("taskId") or "").strip()
            dev_state = body.get("state", "")
            dev_agent = body.get("agentId", "")
            print(
                f"[{AGENT_ID}] Dev callback received (ack only): "
                f"tl_task={team_lead_task_id} dev_task={dev_task_id} "
                f"agent={dev_agent} state={dev_state}"
            )
            self._send_json(200, {"ok": True})
            return

        if path != "/message:send":
            self._send_json(404, {"error": "not_found"})
            return

        body = self._read_body()
        message = body.get("message", {})
        if not message:
            self._send_json(400, {"error": "missing_message"})
            return

        # Resume an INPUT_REQUIRED task
        context_id = (body.get("contextId") or "").strip()
        if context_id:
            prior_task = task_store.get(context_id)
            if prior_task and prior_task.state == "TASK_STATE_INPUT_REQUIRED":
                additional_info = extract_text(message)
                resumed_permissions = (message.get("metadata") or {}).get("permissions")
                if isinstance(resumed_permissions, dict):
                    with _TASK_CONTEXTS_LOCK:
                        ctx = _TASK_CONTEXTS.get(context_id)
                    if ctx is not None:
                        ctx.permissions = resumed_permissions
                with _INPUT_EVENTS_LOCK:
                    entry = _INPUT_EVENTS.get(context_id)
                    if entry:
                        entry["info"] = additional_info
                        entry["event"].set()
                        print(
                            f"[{AGENT_ID}] Resuming INPUT_REQUIRED task {context_id} "
                            f"with info: {additional_info[:100]}"
                        )
                task_store.update_state(context_id, "TASK_STATE_WORKING", "Resumed with user input.")
                self._send_json(200, {"task": prior_task.to_dict()})
                return

        # New task
        metadata = message.get("metadata", {})
        compass_task_id = metadata.get("orchestratorTaskId", "")
        callback_url = metadata.get("orchestratorCallbackUrl", "")
        compass_url = resolve_orchestrator_base_url(metadata, agent_directory=agent_directory)
        workspace = metadata.get("sharedWorkspacePath", "")
        user_text = extract_text(message) or ""

        task = task_store.create()
        ctx = _TaskContext()
        ctx.compass_task_id = compass_task_id
        ctx.compass_callback_url = callback_url
        ctx.compass_url = compass_url
        ctx.shared_workspace_path = workspace
        ctx.permissions = metadata.get("permissions") if isinstance(metadata.get("permissions"), dict) else None
        ctx.original_message = message
        ctx.user_text = user_text

        with _TASK_CONTEXTS_LOCK:
            _TASK_CONTEXTS[task.task_id] = ctx

        audit_log(
            "TASK_RECEIVED",
            task_id=task.task_id,
            compass_task_id=compass_task_id,
            user_text=user_text[:200],
        )

        worker = threading.Thread(
            target=_run_workflow,
            args=(task.task_id, ctx),
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
    """Gracefully stop the HTTP server after a short delay (per-task mode)."""
    def _do_shutdown():
        time.sleep(delay_seconds)
        print(f"[{AGENT_ID}] Per-task shutdown triggered")
        if _SERVER:
            _SERVER.shutdown()

    threading.Thread(target=_do_shutdown, daemon=True).start()


def main():
    global _SERVER
    print(f"[{AGENT_ID}] Team Lead Agent starting on {HOST}:{PORT}")
    agent_directory.start()
    _SERVER = ThreadingHTTPServer((HOST, PORT), TeamLeadHandler)
    reporter.start()
    _SERVER.serve_forever()


if __name__ == "__main__":
    main()
