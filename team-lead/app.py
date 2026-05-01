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

from common.agent_directory import (
    AgentDirectory,
    CapabilityUnavailableError,
    RegistryUnavailableError,
)
from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.launcher import get_launcher
from common.message_utils import artifact_text, build_text_artifact, extract_text
from common.per_task_exit import PerTaskExitHandler
from common.registry_client import RegistryClient
from common.rules_loader import build_system_prompt, load_rules
from common.runtime.adapter import get_runtime, summarize_runtime_configuration
from common.task_store import TaskStore
from common.time_utils import local_clock_time, local_iso_timestamp
from team_lead import prompts

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8030"))
AGENT_ID = os.environ.get("AGENT_ID", "team-lead-agent")
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{AGENT_ID}-local")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://team-lead:{PORT}")
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
COMPASS_URL = os.environ.get("COMPASS_URL", "http://compass:8080")

ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))
TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "3600"))
INPUT_WAIT_TIMEOUT = int(os.environ.get("INPUT_WAIT_TIMEOUT_SECONDS", "7200"))  # 2 hours
MAX_REVIEW_CYCLES = int(os.environ.get("MAX_REVIEW_CYCLES", "2"))
MAX_GATHER_ROUNDS = int(os.environ.get("MAX_GATHER_ROUNDS", "6"))
MAX_INPUT_ROUNDS = int(os.environ.get("MAX_INPUT_ROUNDS", "2"))
SYNC_AGENT_TIMEOUT = int(os.environ.get("SYNC_AGENT_TIMEOUT_SECONDS", "120"))
DEV_AGENT_ACK_TIMEOUT = int(os.environ.get("DEV_AGENT_ACK_TIMEOUT_SECONDS", "3600"))
COMPASS_ACK_TIMEOUT = int(os.environ.get("COMPASS_ACK_TIMEOUT_SECONDS", "300"))

_AGENT_CARD_PATH = os.path.join(os.path.dirname(__file__), "agent-card.json")

registry = RegistryClient()
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

# Events for INPUT_REQUIRED → resume flow
_INPUT_EVENTS: dict[str, dict] = {}  # task_id -> {"event": Event, "info": str | None}
_INPUT_EVENTS_LOCK = threading.Lock()

# Callback events from dev agents: key=(team_lead_task_id:dev_task_id) -> result
_CALLBACK_LOCK = threading.Lock()
_CALLBACK_EVENTS: dict[str, threading.Event] = {}
_CALLBACK_RESULTS: dict[str, dict] = {}

_JIRA_TICKET_KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]+-\d+)\b")
_REPO_URL_RE = re.compile(r"https?://[^\s\"'\}]*?(?:github\.com|bitbucket)[^\s\"'\}]*", re.IGNORECASE)
_FIGMA_URL_RE = re.compile(r"https?://[^\s\"'\}]*figma\.com/[^\s\"'\}]+", re.IGNORECASE)
_STITCH_URL_RE = re.compile(r"https?://[^\s\"'\}]*(?:stitch\.withgoogle\.com|stitch\.googleapis\.com)/[^\s\"'\}]+", re.IGNORECASE)

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

_IMPLEMENTATION_TASK_TYPES = {"feature", "bug_fix", "improvement"}
_TECH_STACK_HINTS = (
    "tech stack",
    "stack",
    "framework",
    "runtime",
    "python",
    "flask",
    "fastapi",
    "django",
    "node",
    "express",
    "nestjs",
    "react",
    "next",
    "vue",
    "typescript",
    "javascript",
)

_DEVELOPMENT_SKILL_NAMES = [
    "constellation-architecture-delivery",
    "constellation-frontend-delivery",
    "constellation-backend-delivery",
    "constellation-database-delivery",
    "constellation-code-review-delivery",
    "constellation-testing-delivery",
    "constellation-ui-evidence-delivery",
]

_GATHER_ACTION_FETCH = "fetch_agent_context"
_GATHER_ACTION_ASK_USER = "ask_user"
_GATHER_ACTION_STOP = "stop"
_GATHER_ACTION_PROCEED = "proceed_to_plan"
_GATHER_FETCH_CAPABILITIES = {
    "jira.ticket.fetch",
    "scm.repo.search",
    "scm.repo.inspect",
    "figma.page.fetch",
    "stitch.project.get",
    "stitch.screen.fetch",
    "stitch.screen.image",
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
        "jira_fetch_attempts",
        "repo_info",
        "design_info",
        "additional_info",
        "plan",
        "dev_result",
        "dev_service_url",
        "dev_task_id",
        "review_result",
        "review_cycles",
        "phases_log",
        "pending_tasks",
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
        self.jira_fetch_attempts: int = 0
        self.repo_info: dict | None = None
        self.design_info: dict | None = None
        self.additional_info: str = ""
        self.plan: dict = {}
        self.dev_result: dict | None = None
        self.dev_service_url: str = ""   # service URL of the active dev agent
        self.dev_task_id: str = ""       # latest task ID on the dev agent
        self.review_result: dict | None = None
        self.review_cycles: int = 0
        self.phases_log: list[str] = []
        self.pending_tasks: list[str] = []


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


def _read_pr_url_from_workspace(workspace: str) -> str:
    """Return the PR URL saved by any execution agent, if available.

    Scans all workspace subdirectories for branch-info.json so that any
    platform agent (android, ios, web, …) is discovered dynamically.
    """
    if not workspace or not os.path.isdir(workspace):
        return ""
    try:
        entries = sorted(os.listdir(workspace))
    except OSError:
        return ""
    for entry in entries:
        path = os.path.join(workspace, entry, "branch-info.json")
        try:
            with open(path, encoding="utf-8") as fh:
                pr_url = json.load(fh).get("prUrl", "")
                if pr_url:
                    return pr_url
        except Exception:
            continue
    return ""


def _post_pr_review_comment(pr_url: str, feedback: str, workspace: str, task_id: str) -> None:
    """Best-effort: post review feedback as a comment on the PR via SCM agent."""
    if not pr_url or not feedback:
        return
    try:
        scm_url = _resolve_agent_service_url("scm.pr.comment")
        if not scm_url:
            print(f"[{AGENT_ID}] No SCM agent with scm.pr.comment capability — skipping PR comment")
            return
        msg = {
            "messageId": f"tl-review-{task_id}",
            "role": "ROLE_USER",
            "parts": [{"text": f"Please add this review comment to PR {pr_url}:\n\n{feedback}"}],
            "metadata": {"prUrl": pr_url, "commentText": feedback},
        }
        rev_task = _a2a_send(scm_url, msg)
        _poll_agent_task(scm_url, rev_task.get("id", ""), timeout=30)
        print(f"[{AGENT_ID}] Posted review comment to PR {pr_url}")
    except Exception as exc:
        print(f"[{AGENT_ID}] Could not post PR review comment: {exc}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def audit_log(event: str, **kwargs):
    entry = {"ts": local_iso_timestamp(), "event": event, **kwargs}
    print(f"[audit] {json.dumps(entry, ensure_ascii=False)}")


def _extract_jira_ticket_key(*texts: str) -> str:
    for text in texts:
        if not text:
            continue
        match = _JIRA_TICKET_KEY_RE.search(text)
        if match:
            return match.group(1).upper()
    return ""


def _is_implementation_request(analysis: dict | None, user_text: str = "") -> bool:
    payload = analysis or {}
    task_type = str(payload.get("task_type") or "").strip().lower()
    if task_type in _IMPLEMENTATION_TASK_TYPES:
        return True

    platform = str(payload.get("platform") or "").strip().lower()
    if platform not in {"web", "android", "ios"}:
        return False

    combined = "\n".join(
        part
        for part in (
            user_text,
            str(payload.get("summary") or ""),
            "\n".join(payload.get("acceptance_criteria") or []),
        )
        if part
    ).lower()
    return any(verb in combined for verb in ("implement", "build", "create", "develop", "fix", "add"))


def _ensure_jira_ticket_for_workflow(analysis: dict, user_text: str) -> dict:
    updated = dict(analysis or {})
    if not _is_implementation_request(updated, user_text):
        return updated

    ticket_key = str(updated.get("jira_ticket_key") or "").strip()
    if not ticket_key:
        ticket_key = _extract_jira_ticket_key(
            user_text,
            str(updated.get("summary") or ""),
            "\n".join(updated.get("acceptance_criteria") or []),
        )
        if ticket_key:
            updated["jira_ticket_key"] = ticket_key
            updated["needs_jira_fetch"] = True

    if ticket_key:
        return updated

    raise RuntimeError(
        "A Jira ticket is required for implementation workflow requests. "
        "Please provide a Jira ticket URL or key before Team Lead can continue."
    )


def _extract_tech_stack_constraints(*texts: str) -> dict:
    combined = "\n".join(text for text in texts if text)
    lower = combined.lower()
    constraints: dict[str, str] = {}

    python_version = re.search(r"python\s*(3(?:\.\d+)*)", lower)
    if python_version:
        constraints["language"] = "python"
        constraints["python_version"] = python_version.group(1)
    elif "python" in lower:
        constraints["language"] = "python"

    if "flask" in lower:
        constraints["backend_framework"] = "flask"
    elif "fastapi" in lower:
        constraints["backend_framework"] = "fastapi"
    elif "django" in lower:
        constraints["backend_framework"] = "django"
    elif "express" in lower:
        constraints["backend_framework"] = "express"
    elif "nestjs" in lower or "nest.js" in lower:
        constraints["backend_framework"] = "nestjs"

    if "next.js" in lower or "nextjs" in lower:
        constraints["frontend_framework"] = "nextjs"
    elif "react" in lower:
        constraints["frontend_framework"] = "react"
    elif "vue" in lower:
        constraints["frontend_framework"] = "vue"

    return constraints


def _has_tech_stack_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(hint in lowered for hint in _TECH_STACK_HINTS)


def _infer_tech_stack_agentic(
    user_text: str,
    ctx: "_TaskContext",
) -> tuple[dict, bool, str]:
    """Infer the implementation tech stack using the agentic runtime.

    Reads Jira ticket content, repository inspection content, and the user
    message — then asks the LLM to identify the language, backend framework,
    and frontend framework with a confidence rating.

    Returns:
        (constraints, needs_clarification, clarification_question)
        - constraints: dict suitable for _apply_tech_stack_confirmation_policy
          (non-empty only when confidence is high or medium)
        - needs_clarification: True when the LLM determined it cannot infer
          the stack and a user question is warranted
        - clarification_question: the specific question to ask the user, or ""
    """
    jira_ctx = (ctx.jira_info or {}).get("content") or ""
    repo_ctx = (ctx.repo_info or {}).get("content") or ""

    # No gathered context yet — fall back to fast keyword scan so we don't
    # burn an LLM call before the gather loop has fetched anything.
    if not jira_ctx and not repo_ctx:
        kw = _extract_tech_stack_constraints(user_text, ctx.additional_info or "")
        return kw, False, ""

    try:
        prompt = prompts.INFER_TECH_STACK_TEMPLATE.format(
            user_text=user_text,
            jira_context=jira_ctx[:8000] if jira_ctx else "(not yet fetched)",
            repo_context=repo_ctx[:4000] if repo_ctx else "(not yet fetched or repo is empty)",
            additional_context=ctx.additional_info or "(none)",
        )
        response = _run_agentic(
            prompt,
            f"[{AGENT_ID}] infer_tech_stack",
            system_prompt=prompts.INFER_TECH_STACK_SYSTEM,
            timeout=60,
        )
        result = _parse_json_from_llm(response)
        if not isinstance(result, dict):
            raise ValueError("LLM returned non-dict for tech stack inference")

        confidence = str(result.get("confidence") or "none").lower()
        evidence = str(result.get("evidence") or "").strip()

        constraints: dict[str, str] = {}
        for field, key in (
            ("language", "language"),
            ("backend_framework", "backend_framework"),
            ("frontend_framework", "frontend_framework"),
            ("build_tool", "build_tool"),
        ):
            val = str(result.get(field) or "").strip().lower()
            if val and val != "null" and val != "other":
                constraints[key] = val

        # Only surface constraints when we have reasonable confidence;
        # low/none means the evidence is too thin to hard-constrain the plan.
        if confidence not in ("high", "medium"):
            constraints = {}

        needs_clarification = (
            bool(result.get("needs_user_clarification"))
            and confidence in ("none", "low")
            and not constraints
        )
        clarification_q = (
            str(result.get("clarification_question") or "").strip()
            if needs_clarification else ""
        )

        if constraints:
            print(
                f"[{AGENT_ID}] Tech stack inferred (confidence={confidence}, "
                f"evidence='{evidence}'): "
                + ", ".join(f"{k}={v}" for k, v in constraints.items())
            )
        elif needs_clarification:
            print(f"[{AGENT_ID}] Tech stack unclear — will ask user: {clarification_q}")
        else:
            print(f"[{AGENT_ID}] Tech stack not determined (confidence={confidence}); proceeding without constraints")

        return constraints, needs_clarification, clarification_q

    except Exception as err:
        # Agentic call failed — fall back to keyword matching so the workflow
        # is never hard-blocked by a runtime error in inference.
        print(f"[{AGENT_ID}] Tech stack agentic inference failed ({err}); falling back to keyword scan")
        kw = _extract_tech_stack_constraints(
            user_text,
            jira_ctx,
            repo_ctx,
            ctx.additional_info or "",
        )
        return kw, False, ""


def _apply_tech_stack_confirmation_policy(
    analysis: dict,
    tech_stack_constraints: dict | None,
    user_text: str = "",
) -> dict:
    updated = dict(analysis or {})
    question = str(updated.get("question_for_user") or "").strip()
    missing = [str(item).strip() for item in (updated.get("missing_info") or []) if str(item).strip()]

    if tech_stack_constraints:
        if _has_tech_stack_signal(question):
            updated["question_for_user"] = None
        updated["missing_info"] = [
            item for item in missing if not _has_tech_stack_signal(item)
        ]
        return updated

    if not _is_implementation_request(updated, user_text):
        return updated
    if str(updated.get("platform") or "").strip().lower() != "web":
        return updated
    if not str(updated.get("jira_ticket_key") or "").strip():
        return updated
    if question and not _has_tech_stack_signal(question):
        return updated

    confirmation_question = (
        "The Jira ticket does not specify the web tech stack. "
        "Please confirm the stack to use, for example Python Flask or Node.js/Express."
    )
    if not any(_has_tech_stack_signal(item) for item in missing):
        missing.insert(0, "confirmed web tech stack")
    updated["missing_info"] = missing
    updated["question_for_user"] = confirmation_question
    return updated


def _render_tech_stack_constraints(constraints: dict | None) -> str:
    if not constraints:
        return "None detected."
    lines = []
    if constraints.get("language") == "python":
        version = constraints.get("python_version")
        lines.append(f"- Language: Python{f' {version}' if version else ''}")
    if constraints.get("backend_framework"):
        lines.append(f"- Backend framework: {constraints['backend_framework']}")
    if constraints.get("frontend_framework"):
        lines.append(f"- Frontend framework: {constraints['frontend_framework']}")
    lines.append("- These constraints override guesses derived from a sparse repo or design context.")
    lines.append("- If the target repo is empty or nearly empty, scaffold the required stack in-place.")
    return "\n".join(lines)


def _enforce_plan_constraints(plan: dict, constraints: dict | None) -> dict:
    if not constraints:
        return plan

    hard_rules = [line for line in _render_tech_stack_constraints(constraints).splitlines() if line.strip()]
    hard_block = "HARD TECH STACK CONSTRAINTS:\n" + "\n".join(hard_rules)
    dev_instruction = (plan.get("dev_instruction") or "").strip()
    if hard_block not in dev_instruction:
        plan["dev_instruction"] = f"{hard_block}\n\n{dev_instruction}".strip()

    acceptance = list(plan.get("acceptance_criteria") or [])
    if constraints.get("language") == "python" and constraints.get("backend_framework") == "flask":
        required = (
            "Implementation uses Python 3.12 and Flask as required by the Jira ticket unless the user explicitly overrides the stack."
        )
        if required not in acceptance:
            acceptance.insert(0, required)
    if acceptance:
        plan["acceptance_criteria"] = acceptance
    plan["tech_stack_constraints"] = constraints
    return plan


_DEV_WORKFLOW_INSTRUCTIONS = (
    "MANDATORY development workflow — follow these steps in order:\n"
    "1. When you start development: transition the Jira ticket to 'In Progress', "
    "assign it to the service account, and add a comment saying you started.\n"
    "2. Implement the feature following the acceptance criteria.\n"
    "3. Write and run tests. Install any missing dependencies at runtime.\n"
    "4. Push your implementation to a feature branch and create a Pull Request "
    "targeting the default branch. Before creating the branch, list existing remote "
    "branches via the SCM agent (scm.branch.list). If the desired branch name already "
    "exists, append '_2', '_3', etc. until a unique name is found.\n"
    "5. After the PR is created: transition the Jira ticket to 'In Review' and "
    "add a comment that includes the PR URL, test status, and a brief summary.\n"
    "All steps are required. Skipping Jira/PR steps is not acceptable."
)


def _build_dev_task_metadata(
    *,
    dev_capability: str,
    compass_task_id: str,
    team_lead_task_id: str,
    workspace: str,
    target_repo_url: str,
    tech_stack_constraints: dict | None,
    acceptance_criteria: list | None,
    requires_tests: bool,
    is_revision: bool = False,
    revision_cycle: int = 0,
    review_issues: list | None = None,
    design_context: dict | None = None,
) -> dict:
    metadata = {
        "requestedCapability": dev_capability,
        "orchestratorTaskId": compass_task_id,
        "orchestratorCallbackUrl": (
            f"{ADVERTISED_URL.rstrip('/')}/tasks/{team_lead_task_id}/callbacks"
        ),
        "sharedWorkspacePath": workspace,
        "teamLeadTaskId": team_lead_task_id,
        "targetRepoUrl": target_repo_url,
        "techStackConstraints": tech_stack_constraints or {},
        "acceptanceCriteria": acceptance_criteria or [],
        "requiresTests": requires_tests,
        "devWorkflowInstructions": _DEV_WORKFLOW_INSTRUCTIONS,
        # Exit rule: dev agent must wait for our ACK before shutting down
        "exitRule": PerTaskExitHandler.build(
            rule_type="wait_for_parent_ack",
            ack_timeout_seconds=DEV_AGENT_ACK_TIMEOUT,
        ),
    }
    if design_context and (design_context.get("content") or design_context.get("url")):
        # Only use the Stitch thumbnail URL when the stitch-design.json came from a
        # get_screen call (identified by the presence of a "screenId" key at the top level).
        # Project-level stitch data only has a project thumbnail which may show a different
        # screen than the one being implemented — never pass that as the design reference.
        _thumbnail_url = ""
        _stitch_path = os.path.join(workspace, "ui-design", "stitch-design.json") if workspace else ""
        if _stitch_path and os.path.isfile(_stitch_path):
            try:
                with open(_stitch_path, encoding="utf-8") as _f:
                    _stitch_data = json.load(_f)
                if _stitch_data.get("screenId"):
                    _image_urls = _stitch_data.get("imageUrls") or []
                    if _image_urls and _image_urls[0]:
                        _thumbnail_url = _image_urls[0]
            except Exception:
                pass
        metadata["designContext"] = {
            "url": design_context.get("url", ""),
            "type": design_context.get("type", ""),
            "content": (design_context.get("content") or "")[:4000],
            "page_name": design_context.get("page_name", ""),
            "thumbnailUrl": _thumbnail_url,
        }
    if is_revision:
        metadata.update(
            {
                "isRevision": True,
                "revisionCycle": revision_cycle,
                "reviewIssues": review_issues or [],
            }
        )
    return metadata


def _ack_agent(service_url: str, task_id: str) -> None:
    """Send ACK to a per-task agent so it can shut down (best-effort)."""
    if not service_url or not task_id:
        return
    request = Request(
        f"{service_url.rstrip('/')}/tasks/{task_id}/ack",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            pass
        print(f"[{AGENT_ID}] ACK sent to {service_url} for task {task_id}")
    except Exception as err:
        print(f"[{AGENT_ID}] Could not ACK agent at {service_url} task {task_id}: {err}")


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


def _task_status_text(task: dict) -> str:
    parts = ((task.get("status") or {}).get("message") or {}).get("parts") or []
    return "\n".join(
        str(part.get("text") or "").strip()
        for part in parts
        if str(part.get("text") or "").strip()
    ).strip()


def _task_artifact_text(task: dict) -> str:
    content = "\n".join(artifact_text(artifact) for artifact in (task.get("artifacts") or [])).strip()
    if content:
        return content
    return _task_status_text(task)


def _require_successful_sync_task(capability: str, task: dict, *, timeout_seconds: int) -> dict:
    state = str((task.get("status") or {}).get("state") or "").strip()
    status_text = _task_status_text(task)
    if state in {"TASK_STATE_COMPLETED", "COMPLETED"}:
        return task
    if state in {"TASK_STATE_FAILED", "FAILED"}:
        detail = f": {status_text}" if status_text else ""
        raise RuntimeError(f"Required capability '{capability}' failed{detail}")
    detail = f" Last status: {status_text}" if status_text else ""
    raise RuntimeError(
        f"Required capability '{capability}' did not complete within sync timeout ({timeout_seconds}s).{detail}"
    )


def _call_sync_agent(
    capability: str,
    message_text: str,
    team_lead_task_id: str,
    workspace_path: str,
    compass_task_id: str,
) -> dict:
    """Call a sync agent (Jira, UI Design) and wait for its result."""
    try:
        _, instance = agent_directory.resolve_capability(capability)
    except RegistryUnavailableError as err:
        raise RuntimeError(
            f"Registry unavailable while resolving required capability '{capability}': {err}"
        ) from err
    except CapabilityUnavailableError as err:
        raise RuntimeError(
            f"Required capability '{capability}' is unavailable. "
            "Boundary systems must be accessed only through registered agents."
        ) from err

    agent_url = (instance or {}).get("service_url", "")
    if not agent_url:
        raise RuntimeError(
            f"Capability '{capability}' is registered but has no routable service URL."
        )

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
        return _require_successful_sync_task(capability, downstream_task, timeout_seconds=0)

    if task_id:
        result = _poll_agent_task(agent_url, task_id, timeout=SYNC_AGENT_TIMEOUT)
        if result:
            return _require_successful_sync_task(
                capability,
                result,
                timeout_seconds=SYNC_AGENT_TIMEOUT,
            )

    raise RuntimeError(
        f"Required capability '{capability}' did not complete within sync timeout ({SYNC_AGENT_TIMEOUT}s)."
    )


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
        agents = agent_directory.find_capability(capability, refresh_on_miss=True)
    except RegistryUnavailableError as err:
        print(f"[{AGENT_ID}] Registry unreachable: {err}")
        return None, None

    if not agents:
        return None, None

    if agents[0].get("execution_mode") == "per-task":
        return agents[0], None

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


def _acquire_dev_agent(capability: str, workflow_task_id: str, *, log_fn=None, role_label: str = "dev agent") -> tuple[dict, dict, str]:
    agent_def, instance = _find_agent_instance(capability)
    if agent_def is None:
        raise RuntimeError(
            f"No agent registered for capability '{capability}'. "
            "Cannot proceed without a matching development agent."
        )

    if instance is None:
        if agent_def.get("execution_mode") == "per-task":
            if log_fn:
                log_fn(f"Launching per-task {role_label} ({agent_def['agent_id']})")
            try:
                launch_info = launcher.launch_instance(agent_def, workflow_task_id)
            except Exception as err:
                raise RuntimeError(
                    f"Failed to launch {role_label} '{agent_def['agent_id']}': {err}"
                ) from err
            instance = _wait_for_idle_instance(
                agent_def["agent_id"],
                launch_info.get("container_name", ""),
                timeout=30,
            )
            if instance is None:
                raise RuntimeError(
                    f"{role_label.capitalize()} '{agent_def['agent_id']}' did not register within 30 s."
                )
        else:
            raise RuntimeError(
                f"Capability '{capability}' is registered but has no idle instances."
            )

    service_url = (instance.get("service_url") or "").rstrip("/")
    if not service_url:
        raise RuntimeError(
            f"{role_label.capitalize()} '{agent_def['agent_id']}' is registered but has no service URL."
        )

    if log_fn:
        log_fn(f"{role_label.capitalize()} ready: {agent_def['agent_id']} at {service_url}")

    return agent_def, instance, service_url


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


def _run_agentic(
    prompt: str,
    actor: str,
    *,
    system_prompt: str | None = None,
    context: dict | None = None,
    model: str | None = None,
    timeout: int = 120,
    max_tokens: int = 4096,
) -> str:
    """Run the configured agentic runtime and return raw text output."""
    result = get_runtime().run(
        prompt=prompt,
        context=context,
        system_prompt=system_prompt,
        model=model,
        timeout=timeout,
        max_tokens=max_tokens,
    )
    for warning in result.get("warnings") or []:
        print(f"[{AGENT_ID}] Runtime warning ({actor}): {warning}")
    return result.get("raw_response") or result.get("summary") or ""


def _build_team_lead_system_prompt(base_prompt: str, *, include_workflow: bool = False) -> str:
    return build_system_prompt(
        base_prompt,
        "team-lead",
        include_workflow=include_workflow,
        skill_names=_DEVELOPMENT_SKILL_NAMES,
    )


def _analyze_task(user_text: str, additional_info: str = "") -> dict:
    additional_context = (
        f"Additional information provided by user:\n{additional_info}"
        if additional_info else ""
    )
    prompt = prompts.ANALYZE_TEMPLATE.format(
        user_text=user_text,
        additional_context=additional_context,
    )
    system = _build_team_lead_system_prompt(prompts.ANALYZE_SYSTEM)
    response = _run_agentic(prompt, f"[{AGENT_ID}] analyze", system_prompt=system)
    return _parse_json_from_llm(response)


def _extract_repo_url(text: str) -> str:
    match = _REPO_URL_RE.search(text or "")
    return match.group().rstrip(".,;)\"'") if match else ""


def _extract_design_reference(text: str) -> tuple[str, str]:
    figma = _FIGMA_URL_RE.search(text or "")
    if figma:
        return figma.group().rstrip(".,;)\"'"), "figma"
    stitch = _STITCH_URL_RE.search(text or "")
    if stitch:
        return stitch.group().rstrip(".,;)\"'"), "stitch"
    return "", ""


def _extract_design_page_name(*texts: str) -> str:
    """Extract a design screen/page name from Jira ticket content or user message."""
    patterns = [
        # Explicit keyword prefix: "page: Landing Page" or "screen: Landing Page"
        re.compile(r"(?:page|screen)(?:\s+name)?\s*[:=-]\s*['\"]?([^\n\r]+?)['\"]?(?:$|[\n\r])", re.IGNORECASE),
        # Quoted: page "Landing Page" or screen 'Landing Page'
        re.compile(r'(?:page|screen)\s+["\']([^"\']+)["\']', re.IGNORECASE),
        # Stitch/Figma JSON label field: "label":"Landing Page (Bare-bones)"
        re.compile(r'"label"\s*:\s*"([^"]+)"'),
        # Parenthetical qualifier: "Landing Page (Bare-bones)" or "Practice Quiz (Full)"
        re.compile(r'\b([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)*)\s*\((?:bare-bones|bare bones|full|minimal|draft|v\d+)\)', re.IGNORECASE),
        # Keyword followed by capitalized phrase
        re.compile(r"(?:page|screen)\s+([A-Z][A-Za-z0-9][^\n\r.]{1,80})", re.IGNORECASE),
    ]
    for text in texts:
        if not text:
            continue
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                page_name = match.group(1).strip().strip("'\" ")
                if page_name and len(page_name) > 3:
                    return page_name
    return ""


def _normalize_design_page_key(value: str) -> str:
    """Normalize design page identifiers so equivalent node labels compare equal."""
    text = str(value or "").strip().lower()
    if not text:
        return ""

    node_match = re.search(r"\bnode(?:[_\s-]*id)?\s*[:=\s-]*([0-9]+)[:\-]([0-9]+)\b", text)
    if node_match:
        return f"node:{node_match.group(1)}-{node_match.group(2)}"

    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _enrich_analysis_from_context(
    analysis: dict,
    jira_info: dict | None,
    design_info: dict | None,
    additional_info: str = "",
) -> dict:
    updated = dict(analysis or {})
    context_parts = []
    if jira_info and jira_info.get("content"):
        context_parts.append(str(jira_info.get("content") or ""))
    if additional_info:
        context_parts.append(additional_info)
    context_blob = "\n\n".join(part for part in context_parts if part)

    if not updated.get("target_repo_url"):
        repo_url = _extract_repo_url(context_blob)
        if repo_url:
            updated["target_repo_url"] = repo_url

    if not design_info and not updated.get("design_url"):
        design_url, design_type = _extract_design_reference(context_blob)
        if design_url:
            updated["design_url"] = design_url
            updated["design_type"] = design_type or updated.get("design_type") or None
            updated["needs_design_context"] = True
    if not updated.get("design_page_name"):
        design_page_name = _extract_design_page_name(context_blob)
        if design_page_name:
            updated["design_page_name"] = design_page_name

    return updated


def _build_design_fetch_request(analysis: dict) -> tuple[str, str, str]:
    design_url = (analysis.get("design_url") or "").strip()
    design_type = (analysis.get("design_type") or "figma").strip() or "figma"
    design_page_name = (analysis.get("design_page_name") or "").strip()
    capability = "stitch.screen.fetch" if design_type == "stitch" else "figma.page.fetch"
    design_message = f"Fetch design from {design_url}"
    if design_page_name:
        design_message += f" page: {design_page_name}"
    return capability, design_message, design_page_name


def _inspect_target_repo(
    team_lead_task_id: str,
    repo_url: str,
    workspace_path: str,
    compass_task_id: str,
) -> dict | None:
    repo_task = _call_sync_agent(
        "scm.repo.inspect",
        f"Inspect repository {repo_url}",
        team_lead_task_id,
        workspace_path,
        compass_task_id,
    )
    content = "\n".join(artifact_text(art) for art in repo_task.get("artifacts", []))
    if not content:
        return None
    return {"repo_url": repo_url, "content": content}


def _available_capability_snapshot(*, force: bool = False) -> dict:
    snapshot = {
        "registryAvailable": True,
        "capabilities": [],
    }
    try:
        agents = agent_directory.list_agents(force=force)
    except RegistryUnavailableError as err:
        snapshot["registryAvailable"] = False
        snapshot["error"] = str(err)
        return snapshot

    indexed: dict[str, dict] = {}
    for agent in agents or []:
        instances = agent.get("instances") or []
        idle_instances = sum(1 for instance in instances if instance.get("status") == "idle")
        for capability in agent.get("capabilities") or []:
            entry = indexed.setdefault(
                capability,
                {
                    "capability": capability,
                    "agentIds": [],
                    "runningInstances": 0,
                    "idleInstances": 0,
                },
            )
            agent_id = str(agent.get("agent_id") or "").strip()
            if agent_id and agent_id not in entry["agentIds"]:
                entry["agentIds"].append(agent_id)
            entry["runningInstances"] += len(instances)
            entry["idleInstances"] += idle_instances

    snapshot["capabilities"] = sorted(indexed.values(), key=lambda item: item["capability"])
    return snapshot


def _capability_names(snapshot: dict) -> set[str]:
    return {
        str(item.get("capability") or "").strip()
        for item in (snapshot.get("capabilities") or [])
        if str(item.get("capability") or "").strip()
    }


def _build_fallback_gather_plan(analysis: dict, ctx: _TaskContext, capability_snapshot: dict) -> dict:
    available = _capability_names(capability_snapshot)
    pending_tasks: list[str] = []
    actions: list[dict] = []

    def _append_fetch(capability: str, pending_text: str, message: str, reason: str) -> None:
        pending_tasks.append(pending_text)
        if not capability_snapshot.get("registryAvailable", True):
            actions.append(
                {
                    "action": _GATHER_ACTION_STOP,
                    "reason": f"Registry is unavailable while resolving required capability '{capability}'.",
                }
            )
            return
        if capability not in available:
            actions.append(
                {
                    "action": _GATHER_ACTION_STOP,
                    "reason": (
                        f"Required capability '{capability}' is unavailable. "
                        "Team Lead cannot continue information gathering without a registered boundary agent."
                    ),
                }
            )
            return
        actions.append(
            {
                "action": _GATHER_ACTION_FETCH,
                "capability": capability,
                "message": message,
                "reason": reason,
            }
        )

    ticket_key = str(analysis.get("jira_ticket_key") or "").strip()
    if analysis.get("needs_jira_fetch") and ticket_key and ctx.jira_info is None:
        _append_fetch(
            "jira.ticket.fetch",
            f"Fetch Jira ticket {ticket_key}",
            f"Fetch ticket {ticket_key}",
            "Need the Jira ticket details before implementation planning.",
        )

    if analysis.get("needs_design_context") and analysis.get("design_url") and ctx.design_info is None:
        capability, message, page_name = _build_design_fetch_request(analysis)
        pending = f"Fetch design from {analysis.get('design_url')}"
        if page_name:
            pending += f" page: {page_name}"
        _append_fetch(
            capability,
            pending,
            message,
            "Need the design specification before implementation planning.",
        )

    repo_url = str(analysis.get("target_repo_url") or "").strip()
    if repo_url and ctx.repo_info is None:
        _append_fetch(
            "scm.repo.inspect",
            f"Inspect repository {repo_url}",
            f"Inspect repository {repo_url}",
            "Need repository context before implementation planning.",
        )

    if actions:
        return {
            "pending_tasks": pending_tasks,
            "actions": actions,
            "summary": "Fetch additional boundary context before planning.",
            "capability_snapshot": capability_snapshot,
        }

    missing = [str(item).strip() for item in (analysis.get("missing_info") or []) if str(item).strip()]
    question = str(analysis.get("question_for_user") or "").strip()
    if missing and question:
        pending_tasks.append(f"Ask user: {question}")
        return {
            "pending_tasks": pending_tasks,
            "actions": [
                {
                    "action": _GATHER_ACTION_ASK_USER,
                    "question": question,
                    "reason": "No further boundary fetch can supply the remaining critical information.",
                }
            ],
            "summary": "Need user clarification before planning.",
            "capability_snapshot": capability_snapshot,
        }

    return {
        "pending_tasks": ["Proceed to implementation planning"],
        "actions": [
            {
                "action": _GATHER_ACTION_PROCEED,
                "reason": "All critical implementation information is available.",
            }
        ],
        "summary": "Ready to create the implementation plan.",
        "capability_snapshot": capability_snapshot,
    }


def _normalize_gather_plan(raw_plan: dict, fallback_plan: dict, capability_snapshot: dict) -> dict:
    available = _capability_names(capability_snapshot)
    pending_tasks = [
        str(item).strip()
        for item in (raw_plan.get("pending_tasks") or [])
        if str(item).strip()
    ]

    normalized_actions: list[dict] = []
    for raw_action in raw_plan.get("actions") or []:
        action = str(raw_action.get("action") or "").strip().lower()
        if action == _GATHER_ACTION_FETCH:
            capability = str(raw_action.get("capability") or "").strip()
            message = str(raw_action.get("message") or "").strip()
            reason = str(raw_action.get("reason") or "").strip()
            if capability in _GATHER_FETCH_CAPABILITIES and capability in available and message:
                normalized_actions.append(
                    {
                        "action": _GATHER_ACTION_FETCH,
                        "capability": capability,
                        "message": message,
                        "reason": reason,
                    }
                )
        elif action == _GATHER_ACTION_ASK_USER:
            question = str(raw_action.get("question") or "").strip()
            reason = str(raw_action.get("reason") or "").strip()
            if question:
                normalized_actions.append(
                    {
                        "action": _GATHER_ACTION_ASK_USER,
                        "question": question,
                        "reason": reason,
                    }
                )
        elif action == _GATHER_ACTION_STOP:
            reason = str(raw_action.get("reason") or "").strip()
            if reason:
                normalized_actions.append(
                    {
                        "action": _GATHER_ACTION_STOP,
                        "reason": reason,
                    }
                )
        elif action == _GATHER_ACTION_PROCEED:
            normalized_actions.append(
                {
                    "action": _GATHER_ACTION_PROCEED,
                    "reason": str(raw_action.get("reason") or "").strip(),
                }
            )

    if any(item["action"] == _GATHER_ACTION_FETCH for item in normalized_actions):
        normalized_actions = [item for item in normalized_actions if item["action"] == _GATHER_ACTION_FETCH]
    elif normalized_actions:
        normalized_actions = [normalized_actions[0]]
    else:
        normalized_actions = list(fallback_plan.get("actions") or [])

    if not pending_tasks:
        pending_tasks = list(fallback_plan.get("pending_tasks") or [])

    return {
        "pending_tasks": pending_tasks,
        "actions": normalized_actions,
        "summary": str(raw_plan.get("summary") or fallback_plan.get("summary") or "").strip(),
        "capability_snapshot": capability_snapshot,
    }


def _select_new_fallback_fetch_actions(attempted_fetch_actions: list[dict], fallback_plan: dict) -> list[dict]:
    attempted = {
        (
            str(item.get("capability") or "").strip(),
            str(item.get("message") or "").strip(),
        )
        for item in attempted_fetch_actions
        if str(item.get("action") or "").strip() == _GATHER_ACTION_FETCH
    }
    return [
        item
        for item in (fallback_plan.get("actions") or [])
        if item.get("action") == _GATHER_ACTION_FETCH
        and (
            str(item.get("capability") or "").strip(),
            str(item.get("message") or "").strip(),
        ) not in attempted
    ]


def _plan_information_gathering(
    user_text: str,
    analysis: dict,
    ctx: _TaskContext,
    *,
    force_refresh: bool = False,
) -> dict:
    capability_snapshot = _available_capability_snapshot(force=force_refresh)
    fallback_plan = _build_fallback_gather_plan(analysis, ctx, capability_snapshot)

    def _trimmed(payload: dict | None) -> str:
        if not payload:
            return "null"
        compact = dict(payload)
        if compact.get("content"):
            compact["content"] = str(compact.get("content") or "")[:2000]
        return json.dumps(compact, ensure_ascii=False, indent=2)

    prompt = prompts.GATHER_TEMPLATE.format(
        user_text=user_text,
        current_analysis=json.dumps(analysis or {}, ensure_ascii=False, indent=2),
        jira_context=_trimmed(ctx.jira_info),
        design_context=_trimmed(ctx.design_info),
        repo_context=_trimmed(ctx.repo_info),
        additional_context=ctx.additional_info or "(none)",
        available_capabilities=json.dumps(capability_snapshot, ensure_ascii=False, indent=2),
    )
    system = _build_team_lead_system_prompt(prompts.GATHER_SYSTEM, include_workflow=True)
    raw_plan = _parse_json_from_llm(
        _run_agentic(prompt, f"[{AGENT_ID}] gather", system_prompt=system)
    )
    return _normalize_gather_plan(raw_plan, fallback_plan, capability_snapshot)


def _save_gather_plan(workspace_path: str, gather_plan: dict) -> None:
    _save_workspace_file(
        workspace_path,
        "team-lead/gather-plan.json",
        json.dumps(
            {
                "pendingTasks": gather_plan.get("pending_tasks") or [],
                "actions": gather_plan.get("actions") or [],
                "summary": gather_plan.get("summary") or "",
                "capabilitySnapshot": gather_plan.get("capability_snapshot") or {},
                "updatedAt": local_iso_timestamp(),
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


def _jira_fetch_succeeded(ctx: _TaskContext) -> bool:
    """Return True only if a Jira ticket was successfully fetched (not a permission/error response)."""
    if not ctx.jira_info or not ctx.jira_info.get("content"):
        return False
    content = str(ctx.jira_info.get("content") or "")
    return "fetch_failed" not in content and "Fetch status: fetch_failed" not in content


def _suppress_redundant_questions(
    analysis: dict,
    ctx: _TaskContext,
    *,
    log_fn=None,
) -> dict:
    updated = dict(analysis or {})
    question = str(updated.get("question_for_user") or "")

    if _jira_fetch_succeeded(ctx):
        jira_keywords = ("jira", "ticket", "url", "browse", "atlassian", "issue", "story", "key")
        if any(keyword in question.lower() for keyword in jira_keywords):
            if log_fn:
                log_fn(f"Suppressing Jira-related question (ticket already fetched): {question}")
            updated["question_for_user"] = None
            updated["missing_info"] = [
                item for item in (updated.get("missing_info") or [])
                if not any(keyword in str(item).lower() for keyword in jira_keywords)
            ]

        repo_keywords = ("repo", "repository", "github", "bitbucket", "clone", "git url", "codebase")
        jira_lower = str(ctx.jira_info.get("content") or "").lower()
        question = str(updated.get("question_for_user") or "")
        if any(keyword in question.lower() for keyword in repo_keywords):
            if any(host in jira_lower for host in ("github.com", "bitbucket")):
                if log_fn:
                    log_fn(f"Suppressing repo URL question (URL found in Jira ticket): {question}")
                updated["question_for_user"] = None
                updated["missing_info"] = [
                    item for item in (updated.get("missing_info") or [])
                    if not any(keyword in str(item).lower() for keyword in repo_keywords)
                ]

        preference_keywords = ("framework", "prefer", "flask", "fastapi", "react", "vue", "which")
        question = str(updated.get("question_for_user") or "")
        if any(keyword in question.lower() for keyword in preference_keywords):
            for framework in ("flask", "fastapi", "react", "next.js", "vue", "django", "express"):
                if framework in jira_lower:
                    if log_fn:
                        log_fn(f"Suppressing framework question ('{framework}' found in ticket): {question}")
                    updated["question_for_user"] = None
                    updated["missing_info"] = [
                        item for item in (updated.get("missing_info") or [])
                        if not any(keyword in str(item).lower() for keyword in preference_keywords)
                    ]
                    break

    if ctx.design_info and ctx.design_info.get("content"):
        question = str(updated.get("question_for_user") or "")
        design_keywords = ("design", "stitch", "figma", "mockup", "wireframe", "screen")
        if any(keyword in question.lower() for keyword in design_keywords):
            if log_fn:
                log_fn(f"Suppressing design-related question (context already fetched): {question}")
            updated["question_for_user"] = None

    if (
        str(updated.get("platform") or "").strip().lower() == "web"
        and ctx.jira_info and ctx.jira_info.get("content")
        and ctx.repo_info and ctx.repo_info.get("content")
        and ctx.design_info and ctx.design_info.get("content")
    ):
        defaultable_ui_keywords = (
            "acceptance criteria",
            "pass/fail",
            "responsive breakpoints",
            "qa step",
            "test/qa",
            "reviewer",
            "owner",
            "assignee",
            "assignment",
        )
        question = str(updated.get("question_for_user") or "")
        if any(keyword in question.lower() for keyword in defaultable_ui_keywords):
            if log_fn:
                log_fn(
                    "Suppressing defaultable UI implementation question "
                    f"(ticket + repo + design context already available): {question}"
                )
            updated["question_for_user"] = None
        updated["missing_info"] = [
            item for item in (updated.get("missing_info") or [])
            if not any(keyword in str(item).lower() for keyword in defaultable_ui_keywords)
        ]

    return updated


def _build_analysis_context(ctx: _TaskContext) -> str:
    parts: list[str] = []
    if ctx.jira_info and ctx.jira_info.get("content"):
        ticket_key = str(ctx.jira_info.get("ticket_key") or "")
        heading = f"Jira ticket {ticket_key}:" if ticket_key else "Jira ticket:"
        # Truncate to avoid exceeding LLM context limits when tickets accumulate many comments.
        jira_content = ctx.jira_info['content'][:30000]
        parts.append(f"{heading}\n{jira_content}")
    if ctx.design_info and ctx.design_info.get("content"):
        parts.append(f"Design context:\n{ctx.design_info['content']}")
    if ctx.repo_info and ctx.repo_info.get("content"):
        parts.append(f"Repository context:\n{ctx.repo_info['content']}")
    if ctx.additional_info:
        parts.append(f"Additional information from user:\n{ctx.additional_info}")
    return "\n\n".join(part for part in parts if part)


def _should_prioritize_stack_question(analysis: dict, ctx: _TaskContext) -> bool:
    question = str(analysis.get("question_for_user") or "").strip().lower()
    if not question or "stack" not in question:
        return False
    if str(analysis.get("target_repo_url") or "").strip():
        return False
    repo_info = ctx.repo_info or {}
    if not repo_info:
        return False
    if str(repo_info.get("content") or "").strip():
        return False
    return True


def _refresh_analysis_with_known_context(
    user_text: str,
    ctx: _TaskContext,
    current_analysis: dict,
    *,
    log_fn=None,
) -> dict:
    combined_context = _build_analysis_context(ctx)
    analysis = dict(current_analysis or {})
    if combined_context:
        if log_fn:
            log_fn("Re-analyzing with gathered context")
        analysis = _analyze_task(user_text, combined_context)
    analysis = _enrich_analysis_from_context(analysis, ctx.jira_info, ctx.design_info, ctx.additional_info)
    return _suppress_redundant_questions(analysis, ctx, log_fn=log_fn)


def _filter_unresolved_missing_info(
    analysis: dict,
    ctx: "_TaskContext",
    tech_stack_constraints: dict | None = None,
) -> list[str]:
    unresolved_missing = [
        str(item).strip()
        for item in (analysis.get("missing_info") or [])
        if str(item).strip()
    ]
    # Filter out missing_info entries for context already gathered during the
    # gather loop. The LLM may still report e.g. "need Stitch layer tree" even
    # though we already have design context in hand.
    if ctx.design_info and ctx.design_info.get("content"):
        design_kws = {"stitch", "figma", "design", "screen", "layer", "asset",
                      "png", "svg", "export", "thumbnail", "permission"}
        unresolved_missing = [
            item for item in unresolved_missing
            if not any(kw in item.lower() for kw in design_kws)
        ]
    if ctx.jira_info and ctx.jira_info.get("content"):
        jira_kws = {"jira", "ticket", "acceptance criteria", "acceptancecriteria",
                    "acceptance_criteria"}
        unresolved_missing = [
            item for item in unresolved_missing
            if not any(kw in item.lower() for kw in jira_kws)
        ]
    if ctx.repo_info and ctx.repo_info.get("content"):
        repo_kws = {"repo", "repository", "branch", "stack", "github",
                    "bitbucket", "language", "manifest"}
        unresolved_missing = [
            item for item in unresolved_missing
            if not any(kw in item.lower() for kw in repo_kws)
        ]
    ci_kws = {"ci ", "ci/", "ci config", "continuous", "workflow", "pipeline",
              "github actions", "gitlab", "jenkins", "circleci", "travis",
              "build system", "build tool", "build config", "target ci"}
    unresolved_missing = [
        item for item in unresolved_missing
        if not any(kw in item.lower() for kw in ci_kws)
    ]
    pr_kws = {"canonical pr", "canonical branch", "authoritative pr", "which pr",
              "which branch", "pr url", "branch name", "merge target"}
    unresolved_missing = [
        item for item in unresolved_missing
        if not any(kw in item.lower() for kw in pr_kws)
    ]
    # Platform / deployment environment details are best-effort and can be
    # handled by the dev agent at runtime.  Filter them out only when the
    # agentic tech-stack inference has already confirmed the stack (i.e.,
    # tech_stack_constraints is non-empty).  When the stack is genuinely
    # unknown the gather loop must surface these items so the user gets asked.
    platform_kws = {
        "platform", "tech-stack", "tech stack", "technology stack",
        "deployment constraint", "environment constraint",
        "deployment detail", "environment detail",
        "hosting environment", "infrastructure",
        "labels/components", "labels and components",
    }
    if tech_stack_constraints:
        # Stack is confirmed — the dev agent can resolve these defaults.
        unresolved_missing = [
            item for item in unresolved_missing
            if not any(kw in item.lower() for kw in platform_kws)
        ]
    elif (
        ctx.jira_info and ctx.jira_info.get("content")
        and ctx.repo_info and ctx.repo_info.get("content")
    ):
        # Both Jira and repo have been inspected and the agentic inference
        # still returned no constraints — the "confirmed tech stack" item in
        # missing_info is intentional and must be kept so the user gets asked.
        # Only filter the pure deployment/environment noise that never blocks planning.
        env_only_kws = {
            "deployment constraint", "environment constraint",
            "deployment detail", "environment detail",
            "hosting environment", "infrastructure",
            "labels/components", "labels and components",
        }
        unresolved_missing = [
            item for item in unresolved_missing
            if not any(kw in item.lower() for kw in env_only_kws)
        ]
    # For UI implementation tasks, the web agent can derive these defaults from
    # the repo and ticket context without blocking plan creation.
    if (
        str(analysis.get("platform") or "").strip().lower() == "web"
        and ctx.jira_info and ctx.jira_info.get("content")
        and ctx.repo_info and ctx.repo_info.get("content")
    ):
        defaultable_web_kws = {
            "dynamic data",
            "api endpoint",
            "api endpoints",
            "mock data",
            "route/path",
            "integration point",
            "where page should be mounted",
            "responsive",
            "breakpoint",
            "browser support",
            "styling approach",
            "styling tooling",
            "plain css",
            "sass",
            "scss",
            "tailwind",
            "css-in-js",
            "component library",
            "design system",
        }
        unresolved_missing = [
            item for item in unresolved_missing
            if not any(kw in item.lower() for kw in defaultable_web_kws)
        ]
    if (
        str(analysis.get("platform") or "").strip().lower() == "web"
        and ctx.jira_info and ctx.jira_info.get("content")
        and ctx.repo_info and ctx.repo_info.get("content")
        and ctx.design_info and ctx.design_info.get("content")
    ):
        defaultable_ui_ticket_kws = {
            "acceptance criteria",
            "pass/fail",
            "responsive breakpoint",
            "qa step",
            "test/qa",
            "reviewer",
            "owner",
            "assignee",
            "assignment",
        }
        unresolved_missing = [
            item for item in unresolved_missing
            if not any(kw in item.lower() for kw in defaultable_ui_ticket_kws)
        ]
    # Organisational conventions and preferences (README templates, PR reviewer
    # assignments, code-owner preferences) are always resolvable by the dev
    # agent using project defaults or industry best practice.  Filter them out
    # whenever both Jira and repo context have already been gathered so that
    # these "nice-to-have" items never block task execution.
    if ctx.jira_info and ctx.jira_info.get("content") and ctx.repo_info and ctx.repo_info.get("content"):
        org_preference_kws = {
            "reviewer",
            "code owner",
            "code reviewer",
            "pr reviewer",
            "preferred reviewer",
            "review assignment",
            "readme",
            "readme template",
            "org-specific",
            "wording requirement",
            "wording requirements",
            "template wording",
            "naming convention",
            "commit convention",
            "commit message convention",
            # PR review state / merge decisions are SCM concerns, not planning blockers
            "review outcome",
            "pr review",
            "approve or request",
            "merge the existing",
            "should i review",
            "shall i review",
        }
        unresolved_missing = [
            item for item in unresolved_missing
            if not any(kw in item.lower() for kw in org_preference_kws)
        ]
    return unresolved_missing


def _execute_gather_action(
    action: dict,
    analysis: dict,
    ctx: _TaskContext,
    *,
    team_lead_task_id: str,
    workspace: str,
    compass_task_id: str,
    log_fn,
) -> bool:
    capability = str(action.get("capability") or "").strip()
    message_text = str(action.get("message") or "").strip()
    reason = str(action.get("reason") or message_text or capability).strip()
    if not capability or capability not in _GATHER_FETCH_CAPABILITIES:
        return False

    def _is_repeat(existing: dict | None, request_text: str) -> bool:
        if not existing:
            return False
        return str(existing.get("request") or "").strip() == request_text

    if capability == "jira.ticket.fetch":
        ticket_key = str(analysis.get("jira_ticket_key") or "").strip()
        request_text = message_text or f"Fetch ticket {ticket_key}"
        # Already have content for this ticket — skip only if the previous fetch SUCCEEDED
        if (
            _jira_fetch_succeeded(ctx)
            and str(ctx.jira_info.get("ticket_key") or "").strip() == ticket_key
        ):
            return False
        # Limit retries for permanently-failed fetches (e.g. permission denied)
        if ctx.jira_fetch_attempts >= 2 and not _jira_fetch_succeeded(ctx):
            return False
        if not ticket_key:
            return False
        log_fn(f"{reason} ({capability})")
        ctx.jira_fetch_attempts += 1
        jira_task = _call_sync_agent(
            capability,
            request_text,
            team_lead_task_id,
            workspace,
            compass_task_id,
        )
        content = _task_artifact_text(jira_task)
        ctx.jira_info = {"ticket_key": ticket_key, "content": content, "request": request_text}
        _save_workspace_file(
            workspace,
            "team-lead/jira-context.json",
            json.dumps(ctx.jira_info, ensure_ascii=False, indent=2),
        )
        log_fn(f"Jira ticket {ticket_key} fetched ({len(content)} chars)")
        return True

    if capability == "scm.repo.search":
        request_text = message_text or reason
        if not request_text or _is_repeat(ctx.repo_info, request_text):
            return False
        log_fn(f"{reason} ({capability})")
        repo_task = _call_sync_agent(
            capability,
            request_text,
            team_lead_task_id,
            workspace,
            compass_task_id,
        )
        content = _task_artifact_text(repo_task)
        repo_url = _extract_repo_url(content) or str(analysis.get("target_repo_url") or "").strip()
        if repo_url and not str(analysis.get("target_repo_url") or "").strip():
            analysis["target_repo_url"] = repo_url
        ctx.repo_info = {"repo_url": repo_url, "content": content, "request": request_text}
        _save_workspace_file(
            workspace,
            "team-lead/repo-context.json",
            json.dumps(ctx.repo_info, ensure_ascii=False, indent=2),
        )
        log_fn(
            f"Repository search context fetched ({len((ctx.repo_info or {}).get('content', ''))} chars)"
        )
        return True

    if capability in {"figma.page.fetch", "stitch.project.get", "stitch.screen.fetch", "stitch.screen.image"}:
        design_url = str(analysis.get("design_url") or "").strip()
        if not design_url:
            return False
        _, fallback_message, page_name = _build_design_fetch_request(analysis)
        request_text = message_text or fallback_message

        # Prevent redundant design fetches:
        # - Any project-level fetch is skipped if we already have ANY design content
        # - Screen-level fetches are skipped if we already fetched at screen level for the same page
        if ctx.design_info and ctx.design_info.get("content"):
            fetched_by = str(ctx.design_info.get("fetchedBy") or "").strip()
            existing_page = str(ctx.design_info.get("page_name") or "").strip()
            normalized_existing_page = _normalize_design_page_key(existing_page)
            normalized_requested_page = _normalize_design_page_key(page_name)
            if capability == "stitch.project.get":
                return False  # already have design context; project-level re-fetch not needed
            if capability in {"stitch.screen.fetch", "figma.page.fetch", "stitch.screen.image"}:
                if fetched_by in {"stitch.screen.fetch", "figma.page.fetch", "stitch.screen.image"}:
                    if not normalized_requested_page or normalized_existing_page == normalized_requested_page:
                        return False  # already fetched this screen
        log_fn(f"{reason} ({capability})")
        design_task = _call_sync_agent(
            capability,
            request_text,
            team_lead_task_id,
            workspace,
            compass_task_id,
        )
        content = _task_artifact_text(design_task)
        ctx.design_info = {
            "url": design_url,
            "type": "stitch" if capability.startswith("stitch.") else "figma",
            "content": content,
            "page_name": page_name,
            "fetchedBy": capability,
            "request": request_text,
        }
        _save_workspace_file(
            workspace,
            "team-lead/design-context.json",
            json.dumps(ctx.design_info, ensure_ascii=False, indent=2),
        )
        log_fn(f"Design context fetched ({len(content)} chars)")
        return True

    if capability == "scm.repo.inspect":
        repo_url = str(analysis.get("target_repo_url") or "").strip()
        request_text = message_text or f"Inspect repository {repo_url}"
        # Already have content for this repo — skip regardless of request wording
        if (
            ctx.repo_info
            and ctx.repo_info.get("content")
            and str(ctx.repo_info.get("repo_url") or "").strip() == repo_url
        ):
            return False
        if not repo_url:
            return False
        log_fn(f"{reason} ({capability})")
        repo_info = _inspect_target_repo(
            team_lead_task_id,
            repo_url,
            workspace,
            compass_task_id,
        )
        ctx.repo_info = repo_info or {"repo_url": repo_url, "content": ""}
        ctx.repo_info["request"] = request_text
        _save_workspace_file(
            workspace,
            "team-lead/repo-context.json",
            json.dumps(ctx.repo_info, ensure_ascii=False, indent=2),
        )
        log_fn(
            f"Repository context fetched ({len((ctx.repo_info or {}).get('content', ''))} chars)"
        )
        return True

    return False


def _is_truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _should_stop_before_dev_dispatch(ctx: _TaskContext) -> bool:
    metadata = ctx.original_message.get("metadata") or {}
    if _is_truthy(metadata.get("stopBeforeDevDispatch")):
        return True
    validation_mode = metadata.get("validationMode") or {}
    if isinstance(validation_mode, dict) and _is_truthy(validation_mode.get("stopBeforeDevDispatch")):
        return True
    return False


def _is_validation_checkpoint_ready(
    ctx: _TaskContext,
    analysis: dict,
    tech_stack_constraints: dict | None,
) -> bool:
    if not _should_stop_before_dev_dispatch(ctx):
        return False

    if analysis.get("needs_jira_fetch") and not ctx.jira_info:
        return False
    if analysis.get("needs_design_context") and not ctx.design_info:
        return False

    missing_items = [str(item).strip().lower() for item in (analysis.get("missing_info") or []) if str(item).strip()]
    question = str(analysis.get("question_for_user") or "").strip().lower()
    stack_missing = any(
        keyword in question
        for keyword in ("stack", "framework", "python", "flask", "react", "node")
    ) or any(
        any(keyword in item for keyword in ("stack", "framework", "python", "flask", "react", "node"))
        for item in missing_items
    )
    if stack_missing and not (tech_stack_constraints or {}):
        return False

    return True


def _create_plan(
    user_text: str,
    jira_info: dict | None,
    repo_info: dict | None,
    design_info: dict | None,
    additional_info: str,
    target_repo_url: str = "",
    tech_stack_constraints: dict | None = None,
) -> dict:
    jira_raw = (
        json.dumps(jira_info, ensure_ascii=False, indent=2)
        if jira_info else ""
    )
    # Truncate Jira content so the plan prompt stays within LLM context limits.
    # Comments/history beyond the first 30K chars are rarely needed for planning.
    jira_ctx = (
        f"Jira ticket details:\n{jira_raw[:30000]}"
        if jira_raw else ""
    )
    design_ctx = (
        f"Design context ({design_info.get('type', 'design')}):\n"
        f"{(design_info.get('content', '') or '')[:2000]}"
        if design_info else ""
    )
    repo_ctx = (
        f"Repository context:\n{(repo_info.get('content', '') or '')[:2000]}"
        if repo_info else ""
    )
    extra_ctx = f"Additional information from user:\n{additional_info}" if additional_info else ""

    prompt = prompts.PLAN_TEMPLATE.format(
        user_text=user_text,
        target_repo_url=target_repo_url or "(not specified)",
        tech_stack_constraints=_render_tech_stack_constraints(tech_stack_constraints),
        jira_context=jira_ctx,
        repo_context=repo_ctx,
        design_context=design_ctx,
        additional_context=extra_ctx,
    )
    system = _build_team_lead_system_prompt(prompts.PLAN_SYSTEM)
    response = _run_agentic(prompt, f"[{AGENT_ID}] plan", system_prompt=system)
    return _enforce_plan_constraints(_parse_json_from_llm(response), tech_stack_constraints)


def _load_workspace_review_evidence(workspace: str) -> str:
    """Load auto-collected workspace artifacts for use in the LLM review context."""
    if not workspace or not os.path.isdir(workspace):
        return ""
    lines: list[str] = []
    try:
        for agent_dir in sorted(os.listdir(workspace)):
            agent_path = os.path.join(workspace, agent_dir)
            if not os.path.isdir(agent_path):
                continue
            pr_path = os.path.join(agent_path, "pr-evidence.json")
            if os.path.isfile(pr_path):
                try:
                    with open(pr_path, encoding="utf-8") as f:
                        data = json.load(f)
                    gen_files = data.get("generatedFiles") or data.get("filesChanged") or []
                    pr_url = data.get("url") or data.get("prUrl") or ""
                    build_passed = data.get("buildPassed")
                    branch = data.get("branch", "")
                    lines.append(f"PR URL: {pr_url}")
                    lines.append(f"Branch: {branch}")
                    lines.append(f"Build passed: {build_passed}")
                    lines.append(f"Generated files committed to PR: {gen_files}")
                except Exception:
                    pass
            tr_path = os.path.join(agent_path, "test-results.json")
            if os.path.isfile(tr_path):
                try:
                    with open(tr_path, encoding="utf-8") as f:
                        data = json.load(f)
                    lines.append(f"Test results: passed={data.get('passed')}, output={str(data.get('output', ''))[:400]}")
                except Exception:
                    pass
            jira_path = os.path.join(agent_path, "jira-actions.json")
            if os.path.isfile(jira_path):
                try:
                    with open(jira_path, encoding="utf-8") as f:
                        data = json.load(f)
                    events = data.get("events") or []
                    completed = [e.get("action") for e in events if e.get("status") == "completed"]
                    lines.append(f"Jira actions completed: {completed}")
                except Exception:
                    pass
            if lines:  # found evidence in this agent dir; stop
                break
    except Exception:
        pass
    return "\n".join(lines)


def _review_output_with_timeout(
    user_text: str,
    plan: dict,
    dev_output: str,
    artifacts: list,
    design_info: dict | None = None,
    workspace: str = "",
    timeout_seconds: int = 300,
) -> dict:
    """Wrapper around _review_output that enforces a hard wall-clock timeout.

    If the LLM call hangs beyond *timeout_seconds*, returns a synthetic
    failure review so the workflow can proceed (accept with noted issues)
    rather than blocking the entire Team Lead container until Compass
    times out the step.
    """
    result_holder: list = []
    exc_holder: list = []

    def _run():
        try:
            result_holder.append(
                _review_output(user_text, plan, dev_output, artifacts,
                               design_info=design_info, workspace=workspace)
            )
        except Exception as exc:  # noqa: BLE001
            exc_holder.append(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds)
    if t.is_alive():
        print(f"[{AGENT_ID}] WARNING: _review_output timed out after {timeout_seconds}s — "
              "accepting output with timeout noted.")
        return {
            "passed": False,
            "score": 0,
            "criteria_results": [],
            "workflow_followed": False,
            "workflow_notes": f"Review timed out after {timeout_seconds}s; could not evaluate.",
            "design_fidelity_checked": False,
            "design_fidelity_notes": "N/A",
            "test_coverage_adequate": False,
            "test_coverage_notes": "Review timed out.",
            "unnecessary_files_in_pr": [],
            "issues": [f"Code review LLM call timed out after {timeout_seconds}s."],
            "missing_requirements": [],
            "feedback_for_dev": (
                "The review timed out before it could evaluate your output. "
                "Please verify all acceptance criteria are met."
            ),
            "summary": f"Review timed out after {timeout_seconds}s — accepting with issues noted.",
        }
    if exc_holder:
        raise exc_holder[0]
    return result_holder[0] if result_holder else {}


def _review_output(
    user_text: str,
    plan: dict,
    dev_output: str,
    artifacts: list,
    design_info: dict | None = None,
    workspace: str = "",
) -> dict:
    criteria_lines = "\n".join(
        f"- {c}" for c in (plan.get("acceptance_criteria") or [])
    ) or "No explicit acceptance criteria defined."
    artifacts_summary = "\n".join(
        f"- {art.get('name', 'artifact')}: {(artifact_text(art) or '')[:400]}"
        for art in (artifacts or [])[:5]
    ) or "No artifacts produced."

    design_context_provided = "No"
    if design_info and design_info.get("content"):
        design_url = design_info.get("url", "")
        design_context_provided = f"Yes — {design_url}" if design_url else "Yes (no URL)"

    workspace_evidence = _load_workspace_review_evidence(workspace) if workspace else ""

    prompt = prompts.REVIEW_TEMPLATE.format(
        user_text=user_text,
        acceptance_criteria=criteria_lines,
        requires_tests=str(plan.get("requires_tests", True)).lower(),
        test_requirements=plan.get("test_requirements") or "Not specified.",
        design_context_provided=design_context_provided,
        dev_output=(dev_output or "No output text.")[:3000],
        artifacts_summary=artifacts_summary,
        workspace_evidence=workspace_evidence or "(none collected)",
    )
    system = _build_team_lead_system_prompt(prompts.REVIEW_SYSTEM, include_workflow=True)
    response = _run_agentic(prompt, f"[{AGENT_ID}] review", system_prompt=system, timeout=240)
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
        return _run_agentic(
            prompt,
            f"[{AGENT_ID}] summarize",
            system_prompt=_build_team_lead_system_prompt(prompts.SUMMARIZE_SYSTEM, include_workflow=True),
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
    runtime_config = {
        "runtime": summarize_runtime_configuration(),
        "rulesLoaded": bool(load_rules("team-lead")),
        "workflowRulesLoaded": bool(load_rules("team-lead", include_workflow=True)),
        "skillPlaybooks": list(_DEVELOPMENT_SKILL_NAMES),
    }

    def log(phase: str):
        ts = local_clock_time()
        entry = f"[{ts}] {phase}"
        ctx.phases_log.append(entry)
        print(f"[{AGENT_ID}][{team_lead_task_id}] {phase}")
        _append_workspace_file(workspace, "team-lead/command-log.txt", entry + "\n")
        _save_workspace_file(
            workspace,
            "team-lead/stage-summary.json",
            json.dumps(
                {
                    "taskId": team_lead_task_id,
                    "agentId": AGENT_ID,
                    "currentPhase": phase,
                    "analysis": ctx.analysis,
                    "hasPlan": bool(ctx.plan),
                    "reviewCycles": ctx.review_cycles,
                    "reviewPassed": (ctx.review_result or {}).get("passed") if ctx.review_result else None,
                    "pendingTasks": ctx.pending_tasks,
                    "runtimeConfig": runtime_config,
                    "updatedAt": local_iso_timestamp(),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        _report_progress(compass_url, compass_task_id, phase)

    try:
        # ── Phase 1: Analyze ─────────────────────────────────────────────────
        task_store.update_state(team_lead_task_id, "ANALYZING", "Analyzing the request…")
        log("Analyzing request")
        analysis = _analyze_task(user_text)
        analysis = _ensure_jira_ticket_for_workflow(analysis, user_text)
        ctx.analysis = analysis
        log(
            f"Analysis complete — type={analysis.get('task_type')}, "
            f"platform={analysis.get('platform')}"
        )

        # ── Phase 2: Gather external info ───────────────────────────────────
        task_store.update_state(team_lead_task_id, "GATHERING_INFO", "Gathering required information…")
        tech_stack_constraints: dict[str, str] = {}
        input_rounds = 0
        needs_reanalysis = False

        for gather_round in range(1, MAX_GATHER_ROUNDS + 1):
            if needs_reanalysis:
                analysis = _refresh_analysis_with_known_context(
                    user_text,
                    ctx,
                    analysis,
                    log_fn=log,
                )

            tech_stack_constraints, _stack_needs_clarification, _stack_clarification_q = (
                _infer_tech_stack_agentic(user_text, ctx)
            )
            if tech_stack_constraints:
                log(
                    "Tech stack inferred: "
                    + ", ".join(f"{key}={value}" for key, value in tech_stack_constraints.items())
                )
            elif _stack_needs_clarification and _stack_clarification_q:
                # Agentic inference could not determine the stack from Jira/repo context;
                # surface it as a user question so the gather loop can pick it up.
                if not _has_tech_stack_signal(str(analysis.get("question_for_user") or "")):
                    analysis["question_for_user"] = _stack_clarification_q
                _missing = [str(i).strip() for i in (analysis.get("missing_info") or []) if str(i).strip()]
                if not any(_has_tech_stack_signal(i) for i in _missing):
                    _missing.insert(0, "confirmed tech stack / framework")
                    analysis["missing_info"] = _missing
                log(f"Tech stack unknown after Jira+repo inspection — queued user question: {_stack_clarification_q}")
            analysis = _apply_tech_stack_confirmation_policy(analysis, tech_stack_constraints, user_text)
            ctx.analysis = analysis

            if _is_validation_checkpoint_ready(ctx, analysis, tech_stack_constraints):
                ctx.pending_tasks = ["Proceed to implementation planning"]
                _save_gather_plan(
                    workspace,
                    {
                        "pending_tasks": ctx.pending_tasks,
                        "actions": [
                            {
                                "action": _GATHER_ACTION_PROCEED,
                                "reason": (
                                    "Validation checkpoint mode has the essential context needed to create the implementation plan."
                                ),
                            }
                        ],
                        "summary": "Ready to create the implementation plan for validation checkpoint.",
                        "capability_snapshot": _available_capability_snapshot(force=(gather_round > 1)),
                    },
                )
                log(
                    f"Gather round {gather_round} pending tasks: "
                    + ", ".join(ctx.pending_tasks)
                )
                break

            gather_plan = _plan_information_gathering(
                user_text,
                analysis,
                ctx,
                force_refresh=(gather_round > 1),
            )
            if _should_prioritize_stack_question(analysis, ctx):
                question = str(analysis.get("question_for_user") or "").strip()
                gather_plan = {
                    "pending_tasks": [f"Ask user: {question}"],
                    "actions": [
                        {
                            "action": _GATHER_ACTION_ASK_USER,
                            "question": question,
                            "reason": (
                                "Repository discovery did not determine the web tech stack; "
                                "ask the user now before continuing lower-priority fetches."
                            ),
                        }
                    ],
                    "summary": "Need user tech stack confirmation before further planning.",
                    "capability_snapshot": gather_plan.get("capability_snapshot") or {},
                }
            ctx.pending_tasks = list(gather_plan.get("pending_tasks") or [])
            _save_gather_plan(workspace, gather_plan)
            log(
                f"Gather round {gather_round} pending tasks: "
                + (", ".join(ctx.pending_tasks) if ctx.pending_tasks else "none")
            )

            actions = list(gather_plan.get("actions") or [])
            if not actions:
                break

            fetch_actions = [
                action for action in actions if action.get("action") == _GATHER_ACTION_FETCH
            ]
            if fetch_actions:
                round_progress = False
                for action in fetch_actions:
                    round_progress = _execute_gather_action(
                        action,
                        analysis,
                        ctx,
                        team_lead_task_id=team_lead_task_id,
                        workspace=workspace,
                        compass_task_id=compass_task_id,
                        log_fn=log,
                    ) or round_progress
                if round_progress:
                    needs_reanalysis = True
                    continue

                fallback_plan = _build_fallback_gather_plan(
                    analysis,
                    ctx,
                    _available_capability_snapshot(force=True),
                )
                fallback_fetch_actions = _select_new_fallback_fetch_actions(fetch_actions, fallback_plan)
                if fallback_fetch_actions:
                    ctx.pending_tasks = list(fallback_plan.get("pending_tasks") or [])
                    _save_gather_plan(workspace, fallback_plan)
                    log("No new boundary context fetched from current plan — retrying with fallback fetch actions")
                    fallback_progress = False
                    for action in fallback_fetch_actions:
                        fallback_progress = _execute_gather_action(
                            action,
                            analysis,
                            ctx,
                            team_lead_task_id=team_lead_task_id,
                            workspace=workspace,
                            compass_task_id=compass_task_id,
                            log_fn=log,
                        ) or fallback_progress
                    if fallback_progress:
                        needs_reanalysis = True
                        continue

                fallback_actions = [
                    item for item in (fallback_plan.get("actions") or [])
                    if item.get("action") != _GATHER_ACTION_FETCH
                ]
                if not fallback_actions:
                    break

                ctx.pending_tasks = list(fallback_plan.get("pending_tasks") or [])
                _save_gather_plan(workspace, fallback_plan)
                log("No new boundary context fetched — falling back to clarification/proceed decision")
                actions = fallback_actions

            next_action = actions[0]
            action_type = next_action.get("action")
            if action_type == _GATHER_ACTION_ASK_USER:
                # Before escalating to the user, check whether the question
                # covers context that was already gathered from boundary agents.
                # If it does, skip it and proceed to planning rather than
                # blocking on an unnecessary clarification round.
                _q_text = str(next_action.get("question") or "").lower()
                _suppress_gather_q = False
                if _jira_fetch_succeeded(ctx):
                    _jira_q_kws = {
                        "acceptance criteria", "acceptancecriteria", "jira", "ticket",
                        "checklist item", "story criteria", "pass/fail", "qa step", "test/qa",
                    }
                    if any(kw in _q_text for kw in _jira_q_kws):
                        log(f"Suppressing gather-plan question (Jira already fetched): {next_action.get('question')}")
                        _suppress_gather_q = True
                if not _suppress_gather_q and ctx.jira_info and ctx.jira_info.get("content") and ctx.repo_info and ctx.repo_info.get("content"):
                    _org_q_kws = {
                        "reviewer", "code owner", "code reviewer", "pr reviewer",
                        "readme template", "readme format", "org-specific",
                        "wording requirement", "wording requirements",
                        "naming convention", "commit convention",
                        # PR state/review outcome is an SCM concern — never block gather with it
                        "review outcome", "pr review", "approve or request",
                        "merge the existing pr", "merge or rework",
                        "should i review", "shall i review",
                        # Existing PR / branch strategy decisions — always proceed with new impl
                        "existing pr", "existing pull request", "open a new pr", "new pr",
                        "update the existing", "create a new pr", "create a new branch",
                        "open new pr", "existing prs", "prior pr",
                        "pr #", "pull request #", "pr number", "pull request number",
                        "whether to open", "whether to create", "should i open",
                        "update or create", "merge or open", "new pull request",
                    }
                    if any(kw in _q_text for kw in _org_q_kws):
                        log(f"Suppressing gather-plan question (jira+repo context available): {next_action.get('question')}")
                        _suppress_gather_q = True
                if _suppress_gather_q:
                    # Treat as proceed: all resolvable context is already in hand.
                    break

                if input_rounds >= MAX_INPUT_ROUNDS:
                    raise RuntimeError(
                        f"Team Lead exceeded the maximum clarification rounds ({MAX_INPUT_ROUNDS})."
                    )
                input_rounds += 1
                question = str(next_action.get("question") or analysis.get("question_for_user") or "").strip()
                if not question:
                    raise RuntimeError("Gather plan requested user clarification without a question.")

                log(f"Missing critical info — asking user: {question}")
                task_store.update_state(team_lead_task_id, "TASK_STATE_INPUT_REQUIRED", question)

                input_event = threading.Event()
                with _INPUT_EVENTS_LOCK:
                    _INPUT_EVENTS[team_lead_task_id] = {"event": input_event, "info": None}

                _notify_compass(
                    callback_url,
                    team_lead_task_id,
                    "TASK_STATE_INPUT_REQUIRED",
                    prompts.INPUT_REQUIRED_PREAMBLE + question,
                )

                if not input_event.wait(timeout=INPUT_WAIT_TIMEOUT):
                    task_store.update_state(
                        team_lead_task_id,
                        "TASK_STATE_FAILED",
                        "Timed out waiting for user input.",
                    )
                    # Register so the finally block's _delayed_cleanup can wait for
                    # Compass ACK (or its timeout) before the container shuts down.
                    exit_handler.register(team_lead_task_id)
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
                task_store.update_state(team_lead_task_id, "ANALYZING", "Re-analyzing with additional information…")
                needs_reanalysis = True
                continue

            if action_type == _GATHER_ACTION_STOP:
                raise RuntimeError(str(next_action.get("reason") or "Team Lead stopped during information gathering."))

            ctx.pending_tasks = []
            break
        else:
            raise RuntimeError(
                f"Team Lead exceeded the maximum information-gathering rounds ({MAX_GATHER_ROUNDS})."
            )

        # ── Phase 4: Plan ────────────────────────────────────────────────────
        unresolved_missing = _filter_unresolved_missing_info(analysis, ctx, tech_stack_constraints)
        if unresolved_missing and not _should_stop_before_dev_dispatch(ctx):
            # Ask the user rather than hard-failing, so transient info gaps
            # (e.g. Jira permission errors, unknown platform) surface as a
            # question instead of a dead-end failure.
            if input_rounds < MAX_INPUT_ROUNDS:
                input_rounds += 1
                question = (
                    "I could not gather all the context needed to start implementation. "
                    "Please clarify: "
                    + "; ".join(unresolved_missing[:3])
                )
                log(f"Unresolved missing info — asking user: {question}")
                task_store.update_state(team_lead_task_id, "TASK_STATE_INPUT_REQUIRED", question)
                input_event = threading.Event()
                with _INPUT_EVENTS_LOCK:
                    _INPUT_EVENTS[team_lead_task_id] = {"event": input_event, "info": None}
                _notify_compass(
                    callback_url,
                    team_lead_task_id,
                    "TASK_STATE_INPUT_REQUIRED",
                    prompts.INPUT_REQUIRED_PREAMBLE + question,
                )
                if not input_event.wait(timeout=INPUT_WAIT_TIMEOUT):
                    task_store.update_state(team_lead_task_id, "TASK_STATE_FAILED",
                                           "Timed out waiting for user input.")
                    exit_handler.register(team_lead_task_id)
                    _notify_compass(callback_url, team_lead_task_id, "TASK_STATE_FAILED",
                                   "Timed out waiting for user input.")
                    return
                with _INPUT_EVENTS_LOCK:
                    entry = _INPUT_EVENTS.pop(team_lead_task_id, {})
                    new_info = entry.get("info") or ""
                ctx.additional_info = (
                    (ctx.additional_info + "\n" + new_info).strip() if ctx.additional_info else new_info
                )
                log(f"User provided additional info for planning: {new_info[:120]}")
                # Re-derive unresolved after user input; if still unresolved, proceed anyway.
                analysis["missing_info"] = []
            else:
                raise RuntimeError(
                    "Cannot create implementation plan with unresolved missing information: "
                    + "; ".join(unresolved_missing[:3])
                )

        task_store.update_state(team_lead_task_id, "PLANNING", "Creating implementation plan…")
        log("Creating implementation plan")

        # Extract repo URL from analysis (may have been pulled from Jira ticket content)
        target_repo_url = (ctx.analysis or {}).get("target_repo_url") or ""
        if not target_repo_url and ctx.jira_info and ctx.jira_info.get("content"):
            # Fall back to regex scan of the Jira ticket body
            _repo_match = re.search(
                r"https?://(?:github\.com|bitbucket\.org)/[^\s/]+/[^\s/\])\"']+",
                ctx.jira_info["content"],
            )
            if _repo_match:
                target_repo_url = _repo_match.group().rstrip(".,;)")
                log(f"Extracted repo URL from Jira ticket: {target_repo_url}")

        plan = _create_plan(
            user_text,
            ctx.jira_info,
            ctx.repo_info,
            ctx.design_info,
            ctx.additional_info,
            target_repo_url,
            tech_stack_constraints=tech_stack_constraints,
        )
        ctx.plan = plan
        dev_capability = plan.get("dev_capability") or "web.task.execute"
        log(
            f"Plan ready — platform={plan.get('platform')}, "
            f"capability={dev_capability}"
        )
        # Save plan to shared workspace
        _save_workspace_file(
            workspace,
            "team-lead/plan.json",
            json.dumps(plan, ensure_ascii=False, indent=2),
        )

        # ── Phase 5: Execute ─────────────────────────────────────────────────
        task_store.update_state(
            team_lead_task_id, "EXECUTING",
            f"Dispatching to {dev_capability}…",
        )
        log(f"Looking up dev agent for capability: {dev_capability}")

        if _should_stop_before_dev_dispatch(ctx):
            checkpoint = {
                "taskId": team_lead_task_id,
                "agentId": AGENT_ID,
                "readyToDispatchCapability": dev_capability,
                "techStackConstraints": tech_stack_constraints,
                "pendingTasks": ctx.pending_tasks,
                "plan": {
                    "platform": plan.get("platform"),
                    "targetRepoUrl": plan.get("target_repo_url") or target_repo_url or "",
                    "acceptanceCriteria": plan.get("acceptance_criteria") or [],
                },
                "updatedAt": local_iso_timestamp(),
            }
            _save_workspace_file(
                workspace,
                "team-lead/pre-dispatch-checkpoint.json",
                json.dumps(checkpoint, ensure_ascii=False, indent=2),
            )
            checkpoint_text = (
                f"Validation checkpoint reached. Team Lead gathered the required context, "
                f"created the implementation plan, and is ready to dispatch {dev_capability}. "
                "The workflow intentionally stopped before launching the development agent."
            )
            checkpoint_artifact = build_text_artifact(
                "team-lead-pre-dispatch-checkpoint",
                checkpoint_text,
                metadata={
                    "agentId": AGENT_ID,
                    "capability": "team-lead.task.analyze",
                    "orchestratorTaskId": compass_task_id,
                    "teamLeadTaskId": team_lead_task_id,
                    "validationCheckpoint": True,
                    "readyToDispatchCapability": dev_capability,
                },
            )
            log("Validation checkpoint reached — stopping before dev dispatch")
            summary = _generate_summary(user_text, ctx.phases_log, "COMPLETED", [checkpoint_artifact])
            _save_workspace_file(workspace, "team-lead/final-summary.md", summary)
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
                    "reviewPassed": True,
                    "validationCheckpoint": True,
                    "readyToDispatchCapability": dev_capability,
                },
            )
            all_artifacts = [final_summary_artifact, checkpoint_artifact]
            task_store.update_state(team_lead_task_id, "TASK_STATE_COMPLETED", summary)
            audit_log(
                "TASK_COMPLETED",
                task_id=team_lead_task_id,
                compass_task_id=compass_task_id,
                validation_checkpoint=True,
            )
            exit_handler.register(team_lead_task_id)
            _notify_compass(callback_url, team_lead_task_id, "TASK_STATE_COMPLETED", summary, all_artifacts)
            return

        agent_def, instance, dev_service_url = _acquire_dev_agent(
            dev_capability,
            team_lead_task_id,
            log_fn=log,
            role_label="dev agent",
        )
        agent_id_str = agent_def["agent_id"]
        instance_id_str = instance["instance_id"]

        try:
            registry.mark_instance_busy(agent_id_str, instance_id_str, team_lead_task_id)
        except Exception:
            pass

        dev_message = {
            "messageId": f"tl-{team_lead_task_id}-dev-{int(time.time())}",
            "role": "ROLE_USER",
            "parts": [{"text": plan.get("dev_instruction") or user_text}],
            "metadata": _build_dev_task_metadata(
                dev_capability=dev_capability,
                compass_task_id=compass_task_id,
                team_lead_task_id=team_lead_task_id,
                workspace=workspace,
                target_repo_url=plan.get("target_repo_url") or target_repo_url or "",
                tech_stack_constraints=tech_stack_constraints,
                acceptance_criteria=plan.get("acceptance_criteria") or [],
                requires_tests=plan.get("requires_tests", False),
                design_context=ctx.design_info,
            ),
        }

        dev_task = _a2a_send(dev_service_url, dev_message)
        dev_task_id = dev_task.get("id", "")
        # Remember service URL and task ID so we can reuse the same container for revisions
        ctx.dev_service_url = dev_service_url
        ctx.dev_task_id = dev_task_id
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

            review = _review_output_with_timeout(
                user_text, plan, dev_output, final_artifacts,
                design_info=ctx.design_info, workspace=workspace,
                timeout_seconds=300,
            )
            ctx.review_result = review
            _save_workspace_file(
                workspace,
                "team-lead/review-notes.json",
                json.dumps(
                    {
                        "taskId": team_lead_task_id,
                        "agentId": AGENT_ID,
                        "cycle": review_cycle + 1,
                        "review": review,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

            passed = review.get("passed", True)
            score = review.get("score")
            if score is None:
                score = "N/A"
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

            # Post review feedback to the PR as a comment (best-effort)
            _pr_url = _read_pr_url_from_workspace(workspace)
            if _pr_url:
                _post_pr_review_comment(_pr_url, feedback, workspace, team_lead_task_id)

            # Reuse the SAME dev agent container (it is still running, waiting for our ACK).
            # Do NOT call _acquire_dev_agent again — that would try to launch a new container
            # which may cause DNS / timing issues while the first container is still alive.
            revision_service_url = ctx.dev_service_url
            if not revision_service_url:
                log("Warning: no dev agent service URL stored; cannot send revision")
                break

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
                "metadata": _build_dev_task_metadata(
                    dev_capability=dev_capability,
                    compass_task_id=compass_task_id,
                    team_lead_task_id=team_lead_task_id,
                    workspace=workspace,
                    target_repo_url=plan.get("target_repo_url") or target_repo_url or "",
                    tech_stack_constraints=tech_stack_constraints,
                    acceptance_criteria=plan.get("acceptance_criteria") or [],
                    requires_tests=plan.get("requires_tests", False),
                    is_revision=True,
                    revision_cycle=review_cycle + 1,
                    review_issues=review.get("issues") or [],
                    design_context=ctx.design_info,
                ),
            }
            rev_task = _a2a_send(revision_service_url, revision_message)
            rev_task_id = rev_task.get("id", "")
            ctx.dev_task_id = rev_task_id  # track latest task ID for final ACK
            log(f"Revision task submitted to same dev agent: {rev_task_id}")

            rev_result = _wait_for_dev_completion(
                team_lead_task_id, rev_task_id, revision_service_url
            )
            if rev_result:
                dev_output = rev_result.get("status_message", dev_output)
                final_artifacts = rev_result.get("artifacts") or final_artifacts
                ctx.dev_result = rev_result
            else:
                log("Warning: revision task timed out, keeping previous output.")

        # After all review cycles are done, ACK the dev agent so it can shut down.
        if ctx.dev_service_url and ctx.dev_task_id:
            log(f"ACK-ing dev agent (task={ctx.dev_task_id}) — releasing it to shut down")
            _ack_agent(ctx.dev_service_url, ctx.dev_task_id)

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
        _save_workspace_file(workspace, "team-lead/final-summary.md", summary)

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
                # True when Team Lead exhausted all review cycles and accepted the output
                # with noted issues. Compass must NOT trigger a completeness retry in this case
                # because Team Lead has already made a deliberate accept-with-defects decision.
                "reviewMaxCyclesReached": ctx.review_cycles >= MAX_REVIEW_CYCLES,
            },
        )
        all_artifacts = [final_summary_artifact] + (final_artifacts or [])

        task_store.update_state(team_lead_task_id, "TASK_STATE_COMPLETED", summary)
        _final_review_passed = (ctx.review_result or {}).get("passed", True)
        if _final_review_passed:
            log("Task completed successfully")
        else:
            log("Task completed with review issues noted (max review cycles reached — proceeding with defects logged)")
        audit_log(
            "TASK_COMPLETED",
            task_id=team_lead_task_id,
            compass_task_id=compass_task_id,
            review_cycles=ctx.review_cycles,
        )
        # Register for Compass ACK BEFORE sending callback (to avoid missing an immediate ACK)
        exit_handler.register(team_lead_task_id)
        _notify_compass(callback_url, team_lead_task_id, "TASK_STATE_COMPLETED", summary, all_artifacts)

    except Exception as err:
        error_text = str(err)
        print(f"[{AGENT_ID}][{team_lead_task_id}] FAILED: {error_text}")
        log(f"FAILED: {error_text[:300]}")

        # ACK dev agent if we failed mid-way (so it doesn't wait forever)
        if ctx.dev_service_url and ctx.dev_task_id:
            _ack_agent(ctx.dev_service_url, ctx.dev_task_id)

        _save_workspace_file(
            workspace,
            "team-lead/review-notes.json",
            json.dumps(
                {
                    "taskId": team_lead_task_id,
                    "agentId": AGENT_ID,
                    "error": error_text,
                    "reviewCycles": ctx.review_cycles,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
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
        # Register for Compass ACK BEFORE sending callback
        exit_handler.register(team_lead_task_id)
        _notify_compass(callback_url, team_lead_task_id, "TASK_STATE_FAILED", failure_summary)

    finally:
        # Wait for Compass ACK (or timeout), then shut down.
        # Team Lead is always a per-task agent — it must always exit after its
        # task completes so stale containers do not accumulate.
        def _delayed_cleanup():
            # Give Compass a short window to poll task state before cleanup
            time.sleep(5)
            with _TASK_CONTEXTS_LOCK:
                _TASK_CONTEXTS.pop(team_lead_task_id, None)
            # Wait for Compass ACK or timeout
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

        # POST /tasks/{id}/ack — Compass confirms it received our callback
        m_ack = re.fullmatch(r"/tasks/([^/]+)/ack", path)
        if m_ack:
            task_id = m_ack.group(1)
            acked = exit_handler.acknowledge(task_id)
            print(f"[{AGENT_ID}] Received ACK from Compass for task {task_id} (registered={acked})")
            self._send_json(200, {"ok": True, "task_id": task_id})
            return

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
    # Bind and listen BEFORE registering with the registry so that Compass can
    # dispatch immediately after the instance appears without getting ECONNREFUSED.
    agent_directory.start()
    _SERVER = ThreadingHTTPServer((HOST, PORT), TeamLeadHandler)
    reporter.start()
    _SERVER.serve_forever()


if __name__ == "__main__":
    main()
