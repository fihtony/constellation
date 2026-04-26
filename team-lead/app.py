"""Team Lead Agent — analyzes tasks, coordinates sub-agents, reviews output.

Responsibilities:
- Analyze incoming tasks from Compass
- Fetch Jira ticket details via Jira Agent when needed
- Fetch design context via UI Design Agent when needed
- Ask Compass to request missing info from user (INPUT_REQUIRED flow)
- Resume the same task when user provides additional info (no new task created)
- Plan and dispatch work to development agents (android, ios, web)
- Review development agent output and request revisions if needed
- Report major progress steps to Compass
- Summarize and finalize the task with callback to Compass
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.launcher import Launcher
from common.llm_client import generate_text
from common.message_utils import artifact_text, build_text_artifact, extract_text
from common.registry_client import RegistryClient
from common.task_store import TaskStore
from team_lead import prompts

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8030"))
AGENT_ID = os.environ.get("AGENT_ID", "team-lead-agent")
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{AGENT_ID}-local")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://team-lead:{PORT}")
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
JIRA_AGENT_URL = os.environ.get("JIRA_AGENT_URL", "http://jira:8010")
UI_DESIGN_AGENT_URL = os.environ.get("UI_DESIGN_AGENT_URL", "http://ui-design:8040")
COMPASS_URL = os.environ.get("COMPASS_URL", "http://compass:8080")

ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))
TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "3600"))
INPUT_WAIT_TIMEOUT = int(os.environ.get("INPUT_WAIT_TIMEOUT_SECONDS", "7200"))  # 2 hours
MAX_REVIEW_CYCLES = int(os.environ.get("MAX_REVIEW_CYCLES", "2"))
SYNC_AGENT_TIMEOUT = int(os.environ.get("SYNC_AGENT_TIMEOUT_SECONDS", "120"))

_AGENT_CARD_PATH = os.path.join(os.path.dirname(__file__), "agent-card.json")

registry = RegistryClient()
launcher = Launcher()
task_store = TaskStore()
reporter = InstanceReporter(
    agent_id=AGENT_ID,
    service_url=ADVERTISED_URL,
    port=PORT,
)

# Per-task internal workflow context (not exposed externally)
_TASK_CONTEXTS: dict[str, "_TaskContext"] = {}
_TASK_CONTEXTS_LOCK = threading.Lock()

# Events for INPUT_REQUIRED → resume flow
_INPUT_EVENTS: dict[str, dict] = {}  # task_id -> {"event": Event, "info": str | None}
_INPUT_EVENTS_LOCK = threading.Lock()

# Callback events from dev agents: key=(team_lead_task_id:dev_task_id) -> result
_CALLBACK_LOCK = threading.Lock()
_CALLBACK_EVENTS: dict[str, threading.Event] = {}
_CALLBACK_RESULTS: dict[str, dict] = {}

NON_TERMINAL_STATES = {
    "SUBMITTED",
    "ANALYZING",
    "GATHERING_INFO",
    "PLANNING",
    "EXECUTING",
    "REVIEWING",
    "COMPLETING",
    "TASK_STATE_WORKING",
    "TASK_STATE_ACCEPTED",
}


class _TaskContext:
    """Internal per-task state for the Team Lead workflow."""

    __slots__ = (
        "compass_task_id",
        "compass_callback_url",
        "compass_url",
        "shared_workspace_path",
        "original_message",
        "user_text",
        "analysis",
        "jira_info",
        "design_info",
        "additional_info",
        "plan",
        "dev_result",
        "review_result",
        "review_cycles",
        "phases_log",
    )

    def __init__(self):
        self.compass_task_id: str = ""
        self.compass_callback_url: str = ""
        self.compass_url: str = COMPASS_URL
        self.shared_workspace_path: str = ""
        self.original_message: dict = {}
        self.user_text: str = ""
        self.analysis: dict = {}
        self.jira_info: dict | None = None
        self.design_info: dict | None = None
        self.additional_info: str = ""
        self.plan: dict = {}
        self.dev_result: dict | None = None
        self.review_result: dict | None = None
        self.review_cycles: int = 0
        self.phases_log: list[str] = []


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def audit_log(event: str, **kwargs):
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kwargs}
    print(f"[audit] {json.dumps(entry, ensure_ascii=False)}")


# ---------------------------------------------------------------------------
# Progress / Callback helpers
# ---------------------------------------------------------------------------

def _report_progress(compass_url: str, compass_task_id: str, step: str):
    """POST a progress step to Compass (best-effort, non-critical)."""
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
# A2A helpers for calling downstream agents
# ---------------------------------------------------------------------------

def _a2a_send(agent_url: str, message: dict, context_id: str | None = None) -> dict:
    """Send a message to another agent; return the downstream task dict."""
    body: dict = {
        "message": message,
        "configuration": {"returnImmediately": True},
    }
    if context_id:
        body["contextId"] = context_id
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{agent_url.rstrip('/')}/message:send",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=ACK_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8")).get("task", {})


def _poll_agent_task(agent_url: str, task_id: str, timeout: int = 60) -> dict | None:
    """Poll an agent's GET /tasks/{id} until terminal state is reached."""
    terminal_states = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED"}
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
                if state in terminal_states:
                    return task
        except Exception:
            pass
        time.sleep(3)
    return None


def _call_sync_agent(
    agent_url: str,
    capability: str,
    message_text: str,
    team_lead_task_id: str,
    workspace_path: str,
    compass_task_id: str,
) -> dict:
    """Call a sync agent (Jira, UI Design) and wait for its result."""
    message = {
        "messageId": f"tl-{team_lead_task_id}-{capability}-{int(time.time())}",
        "role": "ROLE_USER",
        "parts": [{"text": message_text}],
        "metadata": {
            "requestedCapability": capability,
            "orchestratorTaskId": compass_task_id,
            "sharedWorkspacePath": workspace_path,
        },
    }
    downstream_task = _a2a_send(agent_url, message)
    task_id = downstream_task.get("id", "")
    state = downstream_task.get("status", {}).get("state", "")

    terminal_states = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED"}
    if state in terminal_states:
        return downstream_task

    if task_id:
        result = _poll_agent_task(agent_url, task_id, timeout=SYNC_AGENT_TIMEOUT)
        if result:
            return result

    return downstream_task


# ---------------------------------------------------------------------------
# Dev agent callback (async) helpers
# ---------------------------------------------------------------------------

def _register_dev_callback(team_lead_task_id: str, dev_task_id: str) -> tuple[str, threading.Event]:
    key = f"{team_lead_task_id}:{dev_task_id}"
    event = threading.Event()
    with _CALLBACK_LOCK:
        _CALLBACK_EVENTS[key] = event
        if key in _CALLBACK_RESULTS:
            event.set()
    return key, event


def _store_dev_callback_result(team_lead_task_id: str, dev_task_id: str, payload: dict):
    key = f"{team_lead_task_id}:{dev_task_id}"
    with _CALLBACK_LOCK:
        _CALLBACK_RESULTS[key] = payload
        event = _CALLBACK_EVENTS.get(key)
    if event:
        event.set()


def _wait_for_dev_completion(
    team_lead_task_id: str,
    dev_task_id: str,
    dev_service_url: str,
) -> dict | None:
    """Wait for dev agent to complete via callback, with polling fallback."""
    key, event = _register_dev_callback(team_lead_task_id, dev_task_id)
    deadline = time.time() + TASK_TIMEOUT
    next_poll_at = time.time() + 10.0

    try:
        while time.time() < deadline:
            if event.wait(timeout=1.0):
                with _CALLBACK_LOCK:
                    _CALLBACK_EVENTS.pop(key, None)
                    result = _CALLBACK_RESULTS.pop(key, None)
                if result:
                    return result

            if time.time() >= next_poll_at:
                next_poll_at = time.time() + 10.0
                try:
                    request = Request(
                        f"{dev_service_url.rstrip('/')}/tasks/{dev_task_id}",
                        headers={"Accept": "application/json"},
                    )
                    with urlopen(request, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        task = data.get("task", {})
                        state = task.get("status", {}).get("state", "")
                        terminal_states = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED"}
                        if state in terminal_states:
                            text = ""
                            for art in task.get("artifacts", []):
                                text += artifact_text(art) or ""
                            return {
                                "state": state,
                                "status_message": text,
                                "artifacts": task.get("artifacts", []),
                            }
                except Exception:
                    pass

        return None
    finally:
        with _CALLBACK_LOCK:
            _CALLBACK_EVENTS.pop(key, None)
            _CALLBACK_RESULTS.pop(key, None)


# ---------------------------------------------------------------------------
# Registry / launcher helpers
# ---------------------------------------------------------------------------

def _find_agent_instance(capability: str) -> tuple[dict | None, dict | None]:
    """Look up the registry for an agent + idle instance for the capability."""
    try:
        agents = registry.find_by_capability(capability)
    except (URLError, OSError) as err:
        print(f"[{AGENT_ID}] Registry unreachable: {err}")
        return None, None

    if not agents:
        return None, None

    for agent in agents:
        for instance in agent.get("instances", []):
            if instance.get("status") == "idle":
                return agent, instance

    # No idle instance yet; return the definition for on-demand launch
    return agents[0], None


def _wait_for_idle_instance(agent_id: str, container_name: str, timeout: int = 30) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            instances = registry.list_instances(agent_id)
        except Exception:
            instances = []
        for inst in instances:
            if inst.get("container_id") == container_name and inst.get("status") == "idle":
                return inst
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _parse_json_from_llm(text: str) -> dict:
    """Extract a JSON object from LLM response, stripping markdown fences."""
    text = (text or "").strip()
    # Remove markdown code fences
    if text.startswith("```"):
        lines = text.splitlines()
        start = 1  # skip opening fence
        end = len(lines)
        while end > start and lines[end - 1].strip() in ("```", ""):
            end -= 1
        text = "\n".join(lines[start:end]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to find the first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    print(f"[{AGENT_ID}] Warning: could not parse JSON from LLM response: {text[:200]}")
    return {}


def _analyze_task(user_text: str, additional_info: str = "") -> dict:
    additional_context = (
        f"Additional information provided by user:\n{additional_info}"
        if additional_info else ""
    )
    prompt = prompts.ANALYZE_TEMPLATE.format(
        user_text=user_text,
        additional_context=additional_context,
    )
    response = generate_text(prompt, f"[{AGENT_ID}] analyze", system_prompt=prompts.ANALYZE_SYSTEM)
    return _parse_json_from_llm(response)


def _create_plan(
    user_text: str,
    jira_info: dict | None,
    design_info: dict | None,
    additional_info: str,
) -> dict:
    jira_ctx = (
        f"Jira ticket details:\n{json.dumps(jira_info, ensure_ascii=False, indent=2)}"
        if jira_info else ""
    )
    design_ctx = (
        f"Design context ({design_info.get('type', 'design')}):\n"
        f"{(design_info.get('content', '') or '')[:2000]}"
        if design_info else ""
    )
    extra_ctx = f"Additional information from user:\n{additional_info}" if additional_info else ""

    prompt = prompts.PLAN_TEMPLATE.format(
        user_text=user_text,
        jira_context=jira_ctx,
        design_context=design_ctx,
        additional_context=extra_ctx,
    )
    response = generate_text(prompt, f"[{AGENT_ID}] plan", system_prompt=prompts.PLAN_SYSTEM)
    return _parse_json_from_llm(response)


def _review_output(
    user_text: str,
    plan: dict,
    dev_output: str,
    artifacts: list,
) -> dict:
    criteria_lines = "\n".join(
        f"- {c}" for c in (plan.get("acceptance_criteria") or [])
    ) or "No explicit acceptance criteria defined."
    artifacts_summary = "\n".join(
        f"- {art.get('name', 'artifact')}: {(artifact_text(art) or '')[:400]}"
        for art in (artifacts or [])[:5]
    ) or "No artifacts produced."

    prompt = prompts.REVIEW_TEMPLATE.format(
        user_text=user_text,
        acceptance_criteria=criteria_lines,
        test_requirements=plan.get("test_requirements") or "Not specified.",
        dev_output=(dev_output or "No output text.")[:3000],
        artifacts_summary=artifacts_summary,
    )
    response = generate_text(prompt, f"[{AGENT_ID}] review", system_prompt=prompts.REVIEW_SYSTEM)
    return _parse_json_from_llm(response)


def _generate_summary(
    user_text: str,
    phases_log: list[str],
    final_state: str,
    artifacts: list,
) -> str:
    artifacts_text = "\n".join(
        f"- {a.get('name', 'artifact')}: {(artifact_text(a) or '')[:200]}"
        for a in (artifacts or [])[:3]
    ) or "No deliverables recorded."
    phases_text = "\n".join(f"  {p}" for p in (phases_log or [])[-15:]) or "  (no phase log)"

    prompt = prompts.SUMMARIZE_TEMPLATE.format(
        user_text=user_text,
        phases_log=phases_text,
        final_state=final_state,
        artifacts=artifacts_text,
    )
    try:
        return generate_text(
            prompt, f"[{AGENT_ID}] summarize", system_prompt=prompts.SUMMARIZE_SYSTEM
        )
    except Exception as err:
        return f"Task {final_state.lower()}. Summary unavailable: {err}"


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def _run_workflow(team_lead_task_id: str, ctx: _TaskContext):  # noqa: C901
    """
    Full Team Lead workflow running in a background thread.

    Phases:
      ANALYZING → GATHERING_INFO → PLANNING → [INPUT_REQUIRED] →
      EXECUTING → REVIEWING → COMPLETING → TASK_STATE_COMPLETED
    """
    task = task_store.get(team_lead_task_id)
    if not task:
        return

    compass_url = ctx.compass_url
    compass_task_id = ctx.compass_task_id
    callback_url = ctx.compass_callback_url
    workspace = ctx.shared_workspace_path
    user_text = ctx.user_text
    final_artifacts: list = []

    def log(phase: str):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {phase}"
        ctx.phases_log.append(entry)
        print(f"[{AGENT_ID}][{team_lead_task_id}] {phase}")
        _report_progress(compass_url, compass_task_id, phase)

    try:
        # ── Phase 1: Analyze ─────────────────────────────────────────────────
        task_store.update_state(team_lead_task_id, "ANALYZING", "Analyzing the request…")
        log("Analyzing request")
        analysis = _analyze_task(user_text)
        ctx.analysis = analysis
        log(
            f"Analysis complete — type={analysis.get('task_type')}, "
            f"platform={analysis.get('platform')}"
        )

        # ── Phase 2: Gather external info ───────────────────────────────────
        task_store.update_state(team_lead_task_id, "GATHERING_INFO", "Gathering required information…")

        if analysis.get("needs_jira_fetch") and analysis.get("jira_ticket_key"):
            ticket_key = analysis["jira_ticket_key"]
            log(f"Fetching Jira ticket: {ticket_key}")
            try:
                jira_task = _call_sync_agent(
                    JIRA_AGENT_URL,
                    "jira.ticket.fetch",
                    f"Fetch ticket {ticket_key}",
                    team_lead_task_id,
                    workspace,
                    compass_task_id,
                )
                content = "\n".join(
                    artifact_text(art) for art in jira_task.get("artifacts", [])
                )
                ctx.jira_info = {"ticket_key": ticket_key, "content": content}
                log(f"Jira ticket {ticket_key} fetched ({len(content)} chars)")
            except Exception as err:
                log(f"Warning: could not fetch Jira ticket {ticket_key}: {err}")
                ctx.jira_info = {"ticket_key": ticket_key, "content": "", "error": str(err)}

        if analysis.get("needs_design_context") and analysis.get("design_url"):
            design_url = analysis["design_url"]
            design_type = analysis.get("design_type") or "figma"
            capability = (
                "stitch.screen.fetch" if design_type == "stitch" else "figma.page.fetch"
            )
            log(f"Fetching design context ({design_type}): {design_url}")
            try:
                design_task = _call_sync_agent(
                    UI_DESIGN_AGENT_URL,
                    capability,
                    f"Fetch design from {design_url}",
                    team_lead_task_id,
                    workspace,
                    compass_task_id,
                )
                content = "\n".join(
                    artifact_text(art) for art in design_task.get("artifacts", [])
                )
                ctx.design_info = {
                    "url": design_url,
                    "type": design_type,
                    "content": content,
                }
                log(f"Design context fetched ({len(content)} chars)")
            except Exception as err:
                log(f"Warning: could not fetch design from {design_url}: {err}")
                ctx.design_info = {"url": design_url, "type": design_type, "content": "", "error": str(err)}

        # ── Phase 2b: Re-analyze if Jira/design context was gathered ────────
        # The initial analysis (Phase 1) ran before ticket/design data was available.
        # Re-run analysis now so the LLM can clear question_for_user if the ticket
        # already contains enough implementation detail.
        gathered_ctx_parts = []
        if ctx.jira_info and ctx.jira_info.get("content"):
            gathered_ctx_parts.append(f"Jira ticket {ctx.jira_info['ticket_key']}:\n{ctx.jira_info['content']}")
        if ctx.design_info and ctx.design_info.get("content"):
            gathered_ctx_parts.append(f"Design context:\n{ctx.design_info['content']}")
        if gathered_ctx_parts:
            gathered_ctx = "\n\n".join(gathered_ctx_parts)
            combined_additional = (
                (gathered_ctx + "\n\n" + ctx.additional_info).strip()
                if ctx.additional_info
                else gathered_ctx
            )
            log("Re-analyzing with gathered context (Jira/design)")
            analysis = _analyze_task(user_text, combined_additional)
            ctx.analysis = analysis

        # ── Phase 2c: If Jira content was successfully fetched, suppress any
        #    question that merely asks for the Jira URL / ticket content —
        #    we already have it.  This prevents the LLM from triggering an
        #    unnecessary INPUT_REQUIRED round.
        if ctx.jira_info and ctx.jira_info.get("content"):
            question = analysis.get("question_for_user") or ""
            jira_keywords = ("jira", "ticket", "url", "browse", "atlassian", "issue", "story", "key")
            if any(kw in question.lower() for kw in jira_keywords):
                log(f"Suppressing Jira-related question (ticket already fetched): {question}")
                analysis = dict(analysis)
                analysis["question_for_user"] = None
                analysis["missing_info"] = [
                    m for m in (analysis.get("missing_info") or [])
                    if not any(kw in m.lower() for kw in jira_keywords)
                ]
                ctx.analysis = analysis

        # ── Phase 3: Check for missing info (up to 2 INPUT_REQUIRED rounds) ─
        for _input_round in range(2):
            missing = analysis.get("missing_info") or []
            question = analysis.get("question_for_user")
            if not (missing and question):
                break

            log(f"Missing critical info — asking user: {question}")
            task_store.update_state(team_lead_task_id, "TASK_STATE_INPUT_REQUIRED", question)

            # Register resume event
            input_event = threading.Event()
            with _INPUT_EVENTS_LOCK:
                _INPUT_EVENTS[team_lead_task_id] = {"event": input_event, "info": None}

            # Notify Compass — user must reply with additional info
            _notify_compass(
                callback_url,
                team_lead_task_id,
                "TASK_STATE_INPUT_REQUIRED",
                prompts.INPUT_REQUIRED_PREAMBLE + question,
            )

            # Block until user provides info or timeout
            if not input_event.wait(timeout=INPUT_WAIT_TIMEOUT):
                task_store.update_state(
                    team_lead_task_id,
                    "TASK_STATE_FAILED",
                    "Timed out waiting for user input.",
                )
                _notify_compass(
                    callback_url,
                    team_lead_task_id,
                    "TASK_STATE_FAILED",
                    "Timed out waiting for user input.",
                )
                return

            with _INPUT_EVENTS_LOCK:
                entry = _INPUT_EVENTS.pop(team_lead_task_id, {})
                new_info = entry.get("info") or ""

            ctx.additional_info = (
                (ctx.additional_info + "\n" + new_info).strip() if ctx.additional_info else new_info
            )
            log(f"User provided additional info: {new_info[:120]}")

            # Re-analyze with the new information
            task_store.update_state(team_lead_task_id, "ANALYZING", "Re-analyzing with additional information…")
            log("Re-analyzing with updated context")
            analysis = _analyze_task(user_text, ctx.additional_info)
            ctx.analysis = analysis

        # ── Phase 4: Plan ────────────────────────────────────────────────────
        task_store.update_state(team_lead_task_id, "PLANNING", "Creating implementation plan…")
        log("Creating implementation plan")
        plan = _create_plan(user_text, ctx.jira_info, ctx.design_info, ctx.additional_info)
        ctx.plan = plan
        dev_capability = plan.get("dev_capability") or "android.task.execute"
        log(
            f"Plan ready — platform={plan.get('platform')}, "
            f"capability={dev_capability}"
        )

        # ── Phase 5: Execute ─────────────────────────────────────────────────
        task_store.update_state(
            team_lead_task_id, "EXECUTING",
            f"Dispatching to {dev_capability}…",
        )
        log(f"Looking up dev agent for capability: {dev_capability}")

        agent_def, instance = _find_agent_instance(dev_capability)
        if agent_def is None:
            raise RuntimeError(
                f"No agent registered for capability '{dev_capability}'. "
                "Cannot proceed without a matching development agent."
            )

        if instance is None:
            if agent_def.get("execution_mode") == "per-task":
                log(f"Launching per-task dev agent ({agent_def['agent_id']})")
                try:
                    launch_info = launcher.launch_instance(agent_def, team_lead_task_id)
                except Exception as err:
                    raise RuntimeError(
                        f"Failed to launch dev agent '{agent_def['agent_id']}': {err}"
                    ) from err
                instance = _wait_for_idle_instance(
                    agent_def["agent_id"], launch_info.get("container_name", ""), timeout=30
                )
                if instance is None:
                    raise RuntimeError(
                        f"Dev agent '{agent_def['agent_id']}' did not register within 30 s."
                    )
            else:
                raise RuntimeError(
                    f"Capability '{dev_capability}' is registered but has no idle instances."
                )

        dev_service_url = instance["service_url"]
        agent_id_str = agent_def["agent_id"]
        instance_id_str = instance["instance_id"]
        log(f"Dev agent ready: {agent_id_str} at {dev_service_url}")

        try:
            registry.mark_instance_busy(agent_id_str, instance_id_str, team_lead_task_id)
        except Exception:
            pass

        dev_message = {
            "messageId": f"tl-{team_lead_task_id}-dev-{int(time.time())}",
            "role": "ROLE_USER",
            "parts": [{"text": plan.get("dev_instruction") or user_text}],
            "metadata": {
                "requestedCapability": dev_capability,
                "orchestratorTaskId": compass_task_id,
                "orchestratorCallbackUrl": (
                    f"{ADVERTISED_URL.rstrip('/')}/tasks/{team_lead_task_id}/callbacks"
                ),
                "sharedWorkspacePath": workspace,
                "teamLeadTaskId": team_lead_task_id,
                "acceptanceCriteria": plan.get("acceptance_criteria") or [],
                "requiresTests": plan.get("requires_tests", False),
                "devWorkflowInstructions": (
                    "MANDATORY development workflow — follow these steps in order:\n"
                    "1. When you start development: transition the Jira ticket to 'In Progress', "
                    "assign it to the service account, and add a comment saying you started.\n"
                    "2. Implement the feature following the acceptance criteria.\n"
                    "3. Write and run tests. Install any missing dependencies at runtime.\n"
                    "4. Push your implementation to a feature branch and create a Pull Request "
                    "targeting the default branch.\n"
                    "5. After the PR is created: transition the Jira ticket to 'In Review' and "
                    "add a comment that includes the PR URL, test status, and a brief summary.\n"
                    "All steps are required. Skipping Jira/PR steps is not acceptable."
                ),
            },
        }

        dev_task = _a2a_send(dev_service_url, dev_message)
        dev_task_id = dev_task.get("id", "")
        log(f"Dev task submitted: {dev_task_id}")

        # Wait for dev agent completion (callback + polling fallback)
        dev_result = _wait_for_dev_completion(team_lead_task_id, dev_task_id, dev_service_url)
        if dev_result is None:
            raise RuntimeError(
                f"Dev agent '{agent_id_str}' timed out after {TASK_TIMEOUT} s."
            )

        try:
            registry.mark_instance_idle(agent_id_str, instance_id_str)
        except Exception:
            pass

        ctx.dev_result = dev_result
        dev_state = dev_result.get("state", "TASK_STATE_FAILED")
        dev_output = dev_result.get("status_message", "")
        final_artifacts = dev_result.get("artifacts", [])
        log(f"Dev agent completed — state={dev_state}")

        if dev_state in ("TASK_STATE_FAILED", "FAILED"):
            raise RuntimeError(f"Dev agent failed: {(dev_output or '')[:300]}")

        # ── Phase 6: Review ──────────────────────────────────────────────────
        for review_cycle in range(MAX_REVIEW_CYCLES):
            ctx.review_cycles = review_cycle + 1
            task_store.update_state(
                team_lead_task_id, "REVIEWING",
                f"Reviewing output (cycle {review_cycle + 1}/{MAX_REVIEW_CYCLES})…",
            )
            log(f"Reviewing dev output (cycle {review_cycle + 1}/{MAX_REVIEW_CYCLES})")

            review = _review_output(user_text, plan, dev_output, final_artifacts)
            ctx.review_result = review

            passed = review.get("passed", True)
            score = review.get("score", "N/A")
            review_summary = review.get("summary", "")
            log(f"Review result — passed={passed}, score={score}: {review_summary}")

            if passed:
                break

            if review_cycle >= MAX_REVIEW_CYCLES - 1:
                log(
                    f"Max review cycles reached ({MAX_REVIEW_CYCLES}). "
                    "Accepting output with noted issues."
                )
                break

            feedback = review.get("feedback_for_dev") or ""
            log(f"Review failed — sending revision request to dev agent: {feedback[:120]}")

            revision_message = {
                "messageId": f"tl-{team_lead_task_id}-rev-{review_cycle + 1}",
                "role": "ROLE_USER",
                "parts": [
                    {
                        "text": (
                            "Please revise your implementation based on the following "
                            f"code review feedback:\n\n{feedback}"
                        )
                    }
                ],
                "metadata": {
                    "requestedCapability": dev_capability,
                    "orchestratorTaskId": compass_task_id,
                    "orchestratorCallbackUrl": (
                        f"{ADVERTISED_URL.rstrip('/')}/tasks/{team_lead_task_id}/callbacks"
                    ),
                    "sharedWorkspacePath": workspace,
                    "teamLeadTaskId": team_lead_task_id,
                    "isRevision": True,
                    "revisionCycle": review_cycle + 1,
                    "reviewIssues": review.get("issues") or [],
                },
            }
            rev_task = _a2a_send(dev_service_url, revision_message)
            rev_task_id = rev_task.get("id", "")
            log(f"Revision task submitted: {rev_task_id}")

            rev_result = _wait_for_dev_completion(
                team_lead_task_id, rev_task_id, dev_service_url
            )
            if rev_result:
                dev_output = rev_result.get("status_message", dev_output)
                final_artifacts = rev_result.get("artifacts") or final_artifacts
                ctx.dev_result = rev_result
            else:
                log("Warning: revision task timed out, keeping previous output.")

        # ── Phase 7: Update Jira and finalise ───────────────────────────────
        task_store.update_state(team_lead_task_id, "COMPLETING", "Finalizing and summarizing…")

        if ctx.jira_info and (ctx.analysis or {}).get("jira_ticket_key"):
            ticket_key = ctx.analysis["jira_ticket_key"]
            review_note = (
                f"Review: passed={ctx.review_result.get('passed', 'N/A')}, "
                f"cycles={ctx.review_cycles}, "
                f"score={ctx.review_result.get('score', 'N/A')}"
                if ctx.review_result else f"Review cycles: {ctx.review_cycles}"
            )
            comment = (
                f"Team Lead Agent completed implementation.\n"
                f"{review_note}\n"
                f"Output summary: {(dev_output or '')[:400]}"
            )
            try:
                _call_sync_agent(
                    JIRA_AGENT_URL,
                    "jira.comment.add",
                    f"Add comment to ticket {ticket_key}: {comment}",
                    team_lead_task_id,
                    workspace,
                    compass_task_id,
                )
                log(f"Jira ticket {ticket_key} updated with completion comment")
            except Exception as err:
                log(f"Warning: could not update Jira ticket {ticket_key}: {err}")

        log("Generating task summary")
        summary = _generate_summary(user_text, ctx.phases_log, "COMPLETED", final_artifacts)

        final_summary_artifact = build_text_artifact(
            "team-lead-summary",
            summary,
            metadata={
                "agentId": AGENT_ID,
                "capability": "team-lead.task.analyze",
                "orchestratorTaskId": compass_task_id,
                "teamLeadTaskId": team_lead_task_id,
                "platform": (ctx.plan or {}).get("platform", "unknown"),
                "reviewCycles": ctx.review_cycles,
                "reviewPassed": (ctx.review_result or {}).get("passed", True),
            },
        )
        all_artifacts = [final_summary_artifact] + (final_artifacts or [])

        task_store.update_state(team_lead_task_id, "TASK_STATE_COMPLETED", summary)
        log("Task completed successfully")
        audit_log(
            "TASK_COMPLETED",
            task_id=team_lead_task_id,
            compass_task_id=compass_task_id,
            review_cycles=ctx.review_cycles,
        )
        _notify_compass(callback_url, team_lead_task_id, "TASK_STATE_COMPLETED", summary, all_artifacts)

    except Exception as err:
        error_text = str(err)
        print(f"[{AGENT_ID}][{team_lead_task_id}] FAILED: {error_text}")
        log(f"FAILED: {error_text[:300]}")
        try:
            failure_summary = _generate_summary(
                user_text, ctx.phases_log, "FAILED", []
            )
        except Exception:
            failure_summary = f"Task failed: {error_text[:500]}"

        task_store.update_state(team_lead_task_id, "TASK_STATE_FAILED", failure_summary)
        audit_log(
            "TASK_FAILED",
            task_id=team_lead_task_id,
            compass_task_id=compass_task_id,
            error=error_text[:300],
        )
        _notify_compass(callback_url, team_lead_task_id, "TASK_STATE_FAILED", failure_summary)

    finally:
        # Keep context in memory for 1 hour to allow inspection, then clean up
        def _delayed_cleanup():
            time.sleep(3600)
            with _TASK_CONTEXTS_LOCK:
                _TASK_CONTEXTS.pop(team_lead_task_id, None)

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

    # ── GET ──────────────────────────────────────────────────────────────────

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

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path

        # POST /tasks/{id}/callbacks — dev agent notifies completion
        m = re.fullmatch(r"/tasks/([^/]+)/callbacks", path)
        if m:
            team_lead_task_id = m.group(1)
            body = self._read_body()
            dev_task_id = (
                body.get("downstreamTaskId") or body.get("taskId") or ""
            ).strip()
            if not dev_task_id:
                self._send_json(400, {"error": "missing_downstream_task_id"})
                return
            payload = {
                "state": body.get("state", "TASK_STATE_COMPLETED"),
                "status_message": body.get("statusMessage", ""),
                "artifacts": body.get("artifacts") or [],
            }
            _store_dev_callback_result(team_lead_task_id, dev_task_id, payload)
            print(
                f"[{AGENT_ID}] Dev callback received: "
                f"tl_task={team_lead_task_id} dev_task={dev_task_id} state={payload['state']}"
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

        # ── Resume an INPUT_REQUIRED task ────────────────────────────────────
        context_id = (body.get("contextId") or "").strip()
        if context_id:
            prior_task = task_store.get(context_id)
            if prior_task and prior_task.state == "TASK_STATE_INPUT_REQUIRED":
                additional_info = extract_text(message)
                with _INPUT_EVENTS_LOCK:
                    entry = _INPUT_EVENTS.get(context_id)
                    if entry:
                        entry["info"] = additional_info
                        entry["event"].set()
                        print(
                            f"[{AGENT_ID}] Resuming INPUT_REQUIRED task {context_id} "
                            f"with info: {additional_info[:100]}"
                        )
                # Transition task state to WORKING so Compass can see it resumed
                task_store.update_state(context_id, "TASK_STATE_WORKING", "Resumed with user input.")
                self._send_json(200, {"task": prior_task.to_dict()})
                return

        # ── New task ─────────────────────────────────────────────────────────
        metadata = message.get("metadata", {})
        compass_task_id = metadata.get("orchestratorTaskId", "")
        callback_url = metadata.get("orchestratorCallbackUrl", "")
        compass_url = metadata.get("compassUrl") or os.environ.get("COMPASS_URL", COMPASS_URL)
        workspace = metadata.get("sharedWorkspacePath", "")
        user_text = extract_text(message) or ""

        task = task_store.create()
        ctx = _TaskContext()
        ctx.compass_task_id = compass_task_id
        ctx.compass_callback_url = callback_url
        ctx.compass_url = compass_url
        ctx.shared_workspace_path = workspace
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

def main():
    print(f"[{AGENT_ID}] Team Lead Agent starting on {HOST}:{PORT}")
    reporter.start()
    server = ThreadingHTTPServer((HOST, PORT), TeamLeadHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
