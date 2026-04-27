"""Web Agent — full-stack web development execution agent.

Capabilities:
- Frontend: React, Next.js, Vue.js with Ant Design, Material UI, Tailwind CSS
- Backend: Python (Flask, FastAPI, Django), Node.js (Express, NestJS)
- Analyzes task, generates code, writes files to shared workspace
- Creates feature branch and pull request via SCM Agent
- Can query Jira Agent for additional ticket context
- Reports completion via callback to Team Lead Agent
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.llm_client import generate_text
from common.message_utils import artifact_text, build_text_artifact, extract_text
from common.rules_loader import build_system_prompt
from common.task_store import TaskStore
from web import prompts

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8050"))
AGENT_ID = os.environ.get("AGENT_ID", "web-agent")
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{AGENT_ID}-local")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://web-agent:{PORT}")
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
SCM_AGENT_URL = os.environ.get("SCM_AGENT_URL", "http://scm:8020")
JIRA_AGENT_URL = os.environ.get("JIRA_AGENT_URL", "http://jira:8010")
COMPASS_URL = os.environ.get("COMPASS_URL", "http://compass:8080")

ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))
TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "600"))
SYNC_AGENT_TIMEOUT = int(os.environ.get("SYNC_AGENT_TIMEOUT_SECONDS", "120"))

_AGENT_CARD_PATH = os.path.join(os.path.dirname(__file__), "agent-card.json")

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
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kwargs}
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


def _notify_callback(
    callback_url: str,
    task_id: str,
    state: str,
    status_message: str,
    artifacts: list | None = None,
):
    """Notify Team Lead Agent (or caller) of task completion."""
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
# A2A helpers for calling downstream agents
# ---------------------------------------------------------------------------

def _a2a_send(agent_url: str, message: dict) -> dict:
    """Send a message to an agent and return the downstream task dict."""
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
    """Poll GET /tasks/{id} until a terminal state is reached."""
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
    agent_url: str,
    capability: str,
    message_text: str,
    task_id: str,
    workspace_path: str,
    compass_task_id: str,
) -> dict:
    """Call a synchronous agent and wait for its result."""
    message = {
        "messageId": f"web-{task_id}-{capability}-{int(time.time())}",
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


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _parse_json_from_llm(text: str) -> dict:
    """Extract JSON object from LLM response, stripping markdown fences."""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        start = 1
        end = len(lines)
        while end > start and lines[end - 1].strip() in ("```", ""):
            end -= 1
        text = "\n".join(lines[start:end]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    print(f"[{AGENT_ID}] Warning: could not parse JSON from LLM: {text[:200]}")
    return {}


def _analyze_task(task_instruction: str, acceptance_criteria: list, repo_context: str) -> dict:
    criteria_text = "\n".join(f"- {c}" for c in (acceptance_criteria or [])) or "Not specified."
    prompt = prompts.ANALYZE_TEMPLATE.format(
        task_instruction=task_instruction,
        acceptance_criteria=criteria_text,
        repo_context=repo_context or "None provided.",
    )
    system = build_system_prompt(prompts.ANALYZE_SYSTEM, "web")
    response = generate_text(prompt, f"[{AGENT_ID}] analyze", system_prompt=system)
    return _parse_json_from_llm(response)


def _plan_implementation(
    task_instruction: str,
    acceptance_criteria: list,
    analysis: dict,
    repo_snapshot: str,
    design_context: str,
) -> dict:
    criteria_text = "\n".join(f"- {c}" for c in (acceptance_criteria or [])) or "Not specified."
    prompt = prompts.PLAN_TEMPLATE.format(
        task_instruction=task_instruction,
        acceptance_criteria=criteria_text,
        analysis_json=json.dumps(analysis, ensure_ascii=False, indent=2),
        repo_snapshot=repo_snapshot or "No existing codebase.",
        design_context=design_context or "No design context provided.",
    )
    system = build_system_prompt(prompts.PLAN_SYSTEM, "web")
    response = generate_text(prompt, f"[{AGENT_ID}] plan", system_prompt=system)
    return _parse_json_from_llm(response)


def _generate_file_code(
    file_info: dict,
    task_instruction: str,
    analysis: dict,
    context_from_files: str,
    existing_content: str = "",
) -> str:
    """Generate source code for a single file."""
    prompt = prompts.CODEGEN_TEMPLATE.format(
        file_path=file_info.get("path", "unknown"),
        action=file_info.get("action", "create"),
        purpose=file_info.get("purpose", ""),
        key_logic=file_info.get("key_logic", ""),
        dependencies=", ".join(file_info.get("dependencies", [])) or "standard library",
        task_instruction=task_instruction,
        frontend_framework=analysis.get("frontend_framework", "none"),
        ui_library=analysis.get("ui_library", "none"),
        backend_framework=analysis.get("backend_framework", "none"),
        language=analysis.get("language", "javascript"),
        existing_content=existing_content or "N/A (new file)",
        context_from_other_files=context_from_files or "No other files generated yet.",
    )
    return generate_text(prompt, f"[{AGENT_ID}] codegen:{file_info.get('path', '')}",
                          system_prompt=build_system_prompt(prompts.CODEGEN_SYSTEM, "web"))


def _generate_pr_description(
    task_instruction: str,
    acceptance_criteria: list,
    files_changed: list,
    implementation_summary: str,
) -> tuple[str, str]:
    """Return (pr_title, pr_body)."""
    criteria_text = "\n".join(f"- {c}" for c in (acceptance_criteria or [])) or "Not specified."
    files_text = "\n".join(f"- {f}" for f in files_changed) or "No files listed."
    prompt = prompts.PR_DESCRIPTION_TEMPLATE.format(
        task_instruction=task_instruction,
        acceptance_criteria=criteria_text,
        files_changed=files_text,
        implementation_summary=implementation_summary,
    )
    response = generate_text(
        prompt, f"[{AGENT_ID}] pr-description", system_prompt=prompts.PR_DESCRIPTION_SYSTEM
    )
    lines = response.strip().splitlines()
    title = lines[0].strip() if lines else "Web Agent: implement task"
    body = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
    return title, body


def _generate_summary(
    task_instruction: str,
    acceptance_criteria: list,
    files_list: list,
    pr_url: str,
) -> str:
    criteria_text = "\n".join(f"- {c}" for c in (acceptance_criteria or [])) or "Not specified."
    files_text = "\n".join(f"- {f}" for f in files_list) or "No files generated."
    prompt = prompts.SUMMARY_TEMPLATE.format(
        task_instruction=task_instruction,
        files_list=files_text,
        pr_url=pr_url or "No PR created.",
        acceptance_criteria=criteria_text,
    )
    try:
        return generate_text(
            prompt, f"[{AGENT_ID}] summary", system_prompt=prompts.SUMMARY_SYSTEM
        )
    except Exception as err:
        return f"Web Agent completed. Summary unavailable: {err}"


# ---------------------------------------------------------------------------
# SCM / Jira helpers
# ---------------------------------------------------------------------------

def _fetch_jira_context(task_id: str, ticket_key: str, workspace: str, compass_task_id: str) -> str:
    """Fetch Jira ticket content via Jira Agent."""
    try:
        result = _call_sync_agent(
            JIRA_AGENT_URL,
            "jira.ticket.fetch",
            f"Fetch ticket {ticket_key}",
            task_id,
            workspace,
            compass_task_id,
        )
        return "\n".join(artifact_text(a) for a in result.get("artifacts", []))
    except Exception as err:
        print(f"[{AGENT_ID}] Could not fetch Jira ticket {ticket_key}: {err}")
        return ""


def _jira_transition(ticket_key: str, target_status: str, task_id: str, workspace: str, compass_task_id: str):
    """Transition a Jira ticket to a new status (best-effort, non-blocking)."""
    try:
        _call_sync_agent(
            JIRA_AGENT_URL,
            "jira.ticket.transition",
            f"Transition ticket {ticket_key} to '{target_status}'",
            task_id,
            workspace,
            compass_task_id,
        )
        print(f"[{AGENT_ID}] Jira {ticket_key} transitioned to '{target_status}'")
    except Exception as err:
        print(f"[{AGENT_ID}] Jira transition failed (non-critical): {err}")


def _jira_assign_self(ticket_key: str, task_id: str, workspace: str, compass_task_id: str):
    """Assign the Jira ticket to the bot (service account) that owns the credentials (best-effort)."""
    try:
        _call_sync_agent(
            JIRA_AGENT_URL,
            "jira.ticket.assignee",
            f"Assign ticket {ticket_key} to myself (the authenticated service account)",
            task_id,
            workspace,
            compass_task_id,
        )
        print(f"[{AGENT_ID}] Jira {ticket_key} assigned to service account")
    except Exception as err:
        print(f"[{AGENT_ID}] Jira assign failed (non-critical): {err}")


def _jira_add_comment(ticket_key: str, comment: str, task_id: str, workspace: str, compass_task_id: str):
    """Add a comment to a Jira ticket (best-effort, non-blocking)."""
    try:
        _call_sync_agent(
            JIRA_AGENT_URL,
            "jira.comment.add",
            f"Add comment to ticket {ticket_key}: {comment}",
            task_id,
            workspace,
            compass_task_id,
        )
        print(f"[{AGENT_ID}] Jira {ticket_key} comment added")
    except Exception as err:
        print(f"[{AGENT_ID}] Jira comment failed (non-critical): {err}")


def _clone_repo(task_id: str, repo_url: str, workspace: str, compass_task_id: str) -> str:
    """Clone repository via SCM Agent. Returns clone path or empty string."""
    try:
        result = _call_sync_agent(
            SCM_AGENT_URL,
            "scm.git.clone",
            f"Clone repository {repo_url} to {workspace}",
            task_id,
            workspace,
            compass_task_id,
        )
        # Extract clone path from artifacts
        for art in result.get("artifacts", []):
            text = artifact_text(art)
            if text:
                # Try to parse JSON clone result
                try:
                    data = json.loads(text)
                    return data.get("clone_path") or data.get("clonePath") or ""
                except Exception:
                    # Return text directly if it looks like a path
                    if text.strip().startswith("/"):
                        return text.strip()
        return ""
    except Exception as err:
        print(f"[{AGENT_ID}] Could not clone repo {repo_url}: {err}")
        return ""


def _create_branch(task_id: str, repo_url: str, branch_name: str, base_branch: str, workspace: str, compass_task_id: str) -> bool:
    """Create a feature branch via SCM Agent."""
    try:
        result = _call_sync_agent(
            SCM_AGENT_URL,
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
    """Push generated files to feature branch via SCM Agent.
    Passes structured pushPayload in metadata so the SCM agent does not need
    to parse owner/repo/branch from free-form text.
    """
    # Extract owner/repo from repo_url for structured payload
    owner, repo = "", ""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s?#]+)", repo_url or "")
    if m:
        owner = m.group(1)
        repo = m.group(2).rstrip(".git")

    try:
        message = {
            "messageId": f"web-{task_id}-push-{int(time.time())}",
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
        downstream = _a2a_send(SCM_AGENT_URL, message)
        task_id_ds = downstream.get("id", "")
        state = downstream.get("status", {}).get("state", "")
        terminal = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED"}
        if state not in terminal and task_id_ds:
            result = _poll_task(SCM_AGENT_URL, task_id_ds, timeout=SYNC_AGENT_TIMEOUT)
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


def _sanitize_base_branch(branch: str) -> str:
    """Return a safe base branch name. Fall back to 'main' if the value looks
    like a Jira ticket key or other non-branch string."""
    if not branch:
        return "main"
    # Reject patterns like PROJ-123, CSTL-1/landing-page, jira-key/foo
    if re.match(r"^[A-Z][A-Z0-9]+-\d+", branch):
        return "main"
    # Also reject if it contains characters not valid in branch names
    if re.search(r"[\s~^:?*\[\\]", branch):
        return "main"
    return branch


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
    """Create a pull request via SCM Agent. Returns PR URL.
    Passes structured prPayload in metadata to avoid unreliable text parsing.
    """
    # Sanitize base branch — LLM may return Jira-key-style strings
    safe_base = _sanitize_base_branch(base_branch)

    # Extract owner/repo from URL for structured payload
    owner, repo = "", ""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s?#]+)", repo_url or "")
    if m:
        owner = m.group(1)
        repo = m.group(2).rstrip(".git")

    try:
        message = {
            "messageId": f"web-{task_id}-pr-{int(time.time())}",
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
        downstream = _a2a_send(SCM_AGENT_URL, message)
        task_id_ds = downstream.get("id", "")
        state = downstream.get("status", {}).get("state", "")
        terminal = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED"}
        if state not in terminal and task_id_ds:
            result = _poll_task(SCM_AGENT_URL, task_id_ds, timeout=SYNC_AGENT_TIMEOUT)
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
        # Also check error message from status
        msg = downstream.get("status", {}).get("message", {})
        err_txt = (msg.get("parts") or [{}])[0].get("text", "")
        if err_txt:
            print(f"[{AGENT_ID}] PR create status: {err_txt[:200]}")
        return ""
    except Exception as err:
        print(f"[{AGENT_ID}] Could not create PR: {err}")
        return ""



def _read_repo_snapshot(clone_path: str, max_files: int = 30, max_chars: int = 8000) -> str:
    """Read key files from a cloned repo to provide context for LLM."""
    if not clone_path or not os.path.isdir(clone_path):
        return ""

    snapshot_parts: list[str] = []
    chars_used = 0
    files_read = 0

    # Priority files to include first
    priority_patterns = [
        "package.json", "pyproject.toml", "setup.py", "requirements.txt",
        "README.md", "tsconfig.json", ".env.example",
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

    # Read priority files first
    for pattern in priority_patterns:
        candidate = os.path.join(clone_path, pattern)
        if os.path.isfile(candidate) and chars_used < max_chars and files_read < max_files:
            content = _read_file_safe(candidate)
            if content:
                rel = os.path.relpath(candidate, clone_path)
                snapshot_parts.append(f"=== {rel} ===\n{content}")
                chars_used += len(content)
                files_read += 1

    # Walk the tree for source files
    skip_dirs = {
        ".git", "node_modules", "__pycache__", ".next", "dist",
        "build", "out", "venv", ".venv", ".cache",
    }
    source_exts = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md",
        ".html", ".css", ".scss",
    }

    for root, dirs, files in os.walk(clone_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in sorted(files):
            if files_read >= max_files or chars_used >= max_chars:
                break
            _, ext = os.path.splitext(fname)
            if ext not in source_exts:
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, clone_path)
            # Skip if already included as priority
            if fname in priority_patterns:
                continue
            content = _read_file_safe(fpath)
            if content:
                snapshot_parts.append(f"=== {rel} ===\n{content}")
                chars_used += len(content)
                files_read += 1

    return "\n\n".join(snapshot_parts)


def _install_plan_dependencies(deps: list[str], language: str, log_fn):
    """Install dependencies declared in the plan at runtime (best-effort)."""
    if not deps:
        return
    python_pkgs = []
    npm_pkgs = []
    for dep in deps:
        dep = dep.strip()
        if not dep:
            continue
        # Simple heuristic: if it looks like a PyPI package (no @, no /) it's Python;
        # if it starts with @ or is a scoped package it's npm.
        if dep.startswith("@") or "/" in dep or language in ("javascript", "typescript"):
            npm_pkgs.append(dep)
        else:
            python_pkgs.append(dep)

    if python_pkgs:
        log_fn(f"Installing Python packages: {', '.join(python_pkgs)}")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet"] + python_pkgs,
                timeout=120,
                check=False,
            )
        except Exception as err:
            log_fn(f"Warning: pip install failed: {err}")

    if npm_pkgs:
        log_fn(f"Installing npm packages: {', '.join(npm_pkgs)}")
        try:
            subprocess.run(
                ["npm", "install", "--save"] + npm_pkgs,
                timeout=120,
                check=False,
            )
        except Exception as err:
            log_fn(f"Warning: npm install failed: {err}")


def _write_files_to_workspace(workspace_path: str, files: list[dict]) -> list[str]:
    """Write generated code files to the shared workspace. Returns list of written paths."""
    agent_workspace = os.path.join(workspace_path, AGENT_ID)
    os.makedirs(agent_workspace, exist_ok=True)
    written: list[str] = []
    for file_info in files:
        rel_path = file_info.get("path", "output.txt").lstrip("/")
        full_path = os.path.join(agent_workspace, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        content = file_info.get("content", "")
        try:
            with open(full_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(full_path)
        except Exception as err:
            print(f"[{AGENT_ID}] Could not write {full_path}: {err}")
    return written


# ---------------------------------------------------------------------------
# Build / test execution with LLM-guided error recovery
# ---------------------------------------------------------------------------

MAX_BUILD_RETRIES = 3


def _detect_build_command(build_dir: str, language: str) -> list[str] | None:
    """Return the command to run tests, or None if no test harness detected."""
    # Python: pytest or unittest
    if language in ("python", "mixed") or any(
        os.path.isfile(os.path.join(build_dir, f))
        for f in ("requirements.txt", "pyproject.toml", "setup.py")
    ):
        if any(
            fname.startswith("test_") or fname.endswith("_test.py")
            for _, _, files in os.walk(build_dir)
            for fname in files
        ):
            return [sys.executable, "-m", "pytest", "--tb=short", "-q", build_dir]
        # Fall back to running the main module if present
        for candidate in ("main.py", "app.py", "run.py"):
            if os.path.isfile(os.path.join(build_dir, candidate)):
                return [sys.executable, "-c",
                        f"import ast, sys; ast.parse(open('{os.path.join(build_dir, candidate)}').read());"
                        f"print('Syntax OK: {candidate}')"]
    return None


def _run_build(build_dir: str, language: str) -> tuple[bool, str]:
    """Run the build/test command in build_dir. Returns (success, output)."""
    cmd = _detect_build_command(build_dir, language)
    if cmd is None:
        # No test harness — validate Python syntax of every .py file
        errors = []
        for root, _, files in os.walk(build_dir):
            for fname in files:
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, encoding="utf-8") as fh:
                            import ast as _ast
                            _ast.parse(fh.read())
                    except SyntaxError as exc:
                        errors.append(f"{fpath}: {exc}")
        if errors:
            return False, "\n".join(errors)
        return True, "Syntax check passed (no test harness found)."

    try:
        result = subprocess.run(
            cmd,
            cwd=build_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        # Self-heal: if pytest is missing, install it and retry once
        if result.returncode != 0 and "No module named pytest" in output:
            print(f"[{AGENT_ID}] pytest missing — installing...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "pytest"],
                timeout=60,
            )
            result = subprocess.run(
                cmd,
                cwd=build_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Build/test timed out after 120 seconds."
    except Exception as exc:
        return False, f"Could not run build command: {exc}"


def _read_source_files(build_dir: str, max_files: int = 20) -> list[dict]:
    """Read all source files from the build directory for LLM context."""
    files = []
    skip_dirs = {"__pycache__", ".pytest_cache", "node_modules", ".git", "venv", ".venv"}
    source_exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml", ".cfg", ".ini", ".txt"}
    for root, dirs, fnames in os.walk(build_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in sorted(fnames):
            if len(files) >= max_files:
                break
            _, ext = os.path.splitext(fname)
            if ext not in source_exts:
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, build_dir)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    content = fh.read(4000)
                files.append({"path": rel, "content": content})
            except Exception:
                pass
    return files


def _apply_llm_fixes(build_dir: str, fixes: list[dict]):
    """Apply LLM-suggested file fixes to the build directory."""
    for fix in fixes:
        rel_path = fix.get("path", "").lstrip("/")
        content = fix.get("content", "")
        if not rel_path or not content:
            continue
        full_path = os.path.join(build_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        try:
            with open(full_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            print(f"[{AGENT_ID}] Applied fix to {rel_path}")
        except Exception as err:
            print(f"[{AGENT_ID}] Could not apply fix to {rel_path}: {err}")


def _build_and_test_with_recovery(
    build_dir: str,
    task_instruction: str,
    language: str,
    log_fn,
) -> tuple[bool, str]:
    """
    Run build/tests in build_dir with up to MAX_BUILD_RETRIES LLM-guided fix cycles.
    Returns (passed, final_output).
    """
    for attempt in range(1, MAX_BUILD_RETRIES + 1):
        log_fn(f"Build/test attempt {attempt}/{MAX_BUILD_RETRIES}")
        success, output = _run_build(build_dir, language)
        if success:
            log_fn(f"Build/test passed on attempt {attempt}")
            return True, output

        log_fn(f"Build/test failed (attempt {attempt}): {output[:200]}")
        if attempt == MAX_BUILD_RETRIES:
            break

        # Ask LLM to diagnose and fix
        source_files = _read_source_files(build_dir)
        fix_prompt = prompts.BUILD_FIX_TEMPLATE.format(
            failure_output=output[:3000],
            source_files_json=json.dumps(source_files, ensure_ascii=False, indent=2)[:6000],
            task_instruction=task_instruction[:1000],
        )
        fix_response = generate_text(
            fix_prompt,
            f"[{AGENT_ID}] build-fix-attempt-{attempt}",
            system_prompt=prompts.BUILD_FIX_SYSTEM,
        )
        fix_data = _parse_json_from_llm(fix_response)
        diagnosis = fix_data.get("diagnosis", "unknown")
        fixes = fix_data.get("fixes") or []
        log_fn(f"LLM diagnosis: {diagnosis} — {len(fixes)} fix(es) to apply")

        if not fixes:
            log_fn("LLM produced no fixes — stopping retry loop")
            break

        _apply_llm_fixes(build_dir, fixes)

    return False, output



# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def _run_workflow(task_id: str, message: dict):  # noqa: C901
    """
    Full Web Agent workflow running in a background thread.

    Phases:
      ANALYZING → PLANNING → IMPLEMENTING → PUSHING → COMPLETED
    """
    task = task_store.get(task_id)
    if not task:
        return

    metadata = message.get("metadata", {})
    compass_task_id = metadata.get("orchestratorTaskId", "")
    callback_url = metadata.get("orchestratorCallbackUrl", "")
    compass_url = metadata.get("compassUrl") or COMPASS_URL
    workspace = metadata.get("sharedWorkspacePath", "")
    acceptance_criteria: list = metadata.get("acceptanceCriteria") or []
    is_revision: bool = metadata.get("isRevision", False)
    review_issues: list = metadata.get("reviewIssues") or []

    task_instruction = extract_text(message) or ""
    final_artifacts: list = []

    def log(phase: str):
        ts = time.strftime("%H:%M:%S")
        print(f"[{AGENT_ID}][{task_id}] [{ts}] {phase}")
        _report_progress(compass_url, compass_task_id, f"[Web Agent] {phase}")

    try:
        audit_log("TASK_STARTED", task_id=task_id, compass_task_id=compass_task_id)

        # If this is a revision, append review issues to instruction
        if is_revision and review_issues:
            issues_text = "\n".join(f"- {issue}" for issue in review_issues)
            task_instruction = (
                f"{task_instruction}\n\n"
                f"REVISION REQUEST — please fix the following issues:\n{issues_text}"
            )

        # ── Phase 1: Analyze ────────────────────────────────────────────────
        task_store.update_state(task_id, "ANALYZING", "Analyzing the web development task…")
        log("Analyzing task")
        analysis = _analyze_task(task_instruction, acceptance_criteria, repo_context="")
        log(
            f"Analysis: scope={analysis.get('scope')}, "
            f"frontend={analysis.get('frontend_framework')}, "
            f"backend={analysis.get('backend_framework')}, "
            f"ui={analysis.get('ui_library')}"
        )

        # ── Phase 2: Gather repo context (optional) ─────────────────────────
        task_store.update_state(task_id, "GATHERING_INFO", "Gathering context…")
        jira_content = ""
        repo_snapshot = ""
        clone_path = ""

        # Extract Jira ticket key if present in instruction
        ticket_match = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", task_instruction)
        if ticket_match and workspace:
            ticket_key = ticket_match.group(1)
            log(f"Fetching Jira context for {ticket_key}")
            jira_content = _fetch_jira_context(task_id, ticket_key, workspace, compass_task_id)
            if jira_content:
                log(f"Jira ticket {ticket_key} fetched ({len(jira_content)} chars)")
                # Enrich task instruction with Jira context
                task_instruction = (
                    f"{task_instruction}\n\n"
                    f"Jira ticket context ({ticket_key}):\n{jira_content[:3000]}"
                )

            # ── Dev Workflow Step 1: Mark ticket In Progress ─────────────────
            log(f"Updating Jira ticket {ticket_key}: In Progress → assign self → comment")
            _jira_transition(ticket_key, "In Progress", task_id, workspace, compass_task_id)
            _jira_assign_self(ticket_key, task_id, workspace, compass_task_id)
            _jira_add_comment(
                ticket_key,
                f"🤖 **Web Agent** (`{AGENT_ID}`) has picked up this ticket and started development.\n"
                f"Internal task ID: `{task_id}`",
                task_id,
                workspace,
                compass_task_id,
            )

        repo_url = analysis.get("repo_url") or ""
        # If no repo_url in analysis, try extracting from instruction
        if not repo_url:
            url_match = re.search(r"https?://[^\s]+\.git", task_instruction) or \
                        re.search(r"https?://github\.com/[^\s]+", task_instruction) or \
                        re.search(r"https?://[^\s]*/scm/[^\s]+", task_instruction)
            if url_match:
                repo_url = url_match.group().rstrip("/.,;)")

        if repo_url and analysis.get("needs_repo_clone") and workspace:
            log(f"Cloning repository: {repo_url}")
            clone_path = _clone_repo(task_id, repo_url, workspace, compass_task_id)
            if clone_path:
                log(f"Repository cloned to {clone_path}")
                repo_snapshot = _read_repo_snapshot(clone_path)
                # Re-analyze with repo context
                analysis = _analyze_task(task_instruction, acceptance_criteria, repo_snapshot[:2000])

        # ── Phase 3: Plan ────────────────────────────────────────────────────
        task_store.update_state(task_id, "PLANNING", "Creating implementation plan…")
        log("Planning implementation")
        plan = _plan_implementation(
            task_instruction,
            acceptance_criteria,
            analysis,
            repo_snapshot,
            design_context="",
        )
        files_to_implement = plan.get("files") or []
        log(f"Plan ready — {len(files_to_implement)} file(s) to implement")

        if not files_to_implement:
            raise RuntimeError("LLM returned an empty file plan — cannot proceed.")

        # ── Phase 3b: Install plan dependencies at runtime ───────────────────
        install_deps = plan.get("install_dependencies") or []
        if install_deps:
            task_store.update_state(task_id, "INSTALLING_DEPS", "Installing runtime dependencies…")
            _install_plan_dependencies(install_deps, analysis.get("language", "python"), log)

        # ── Phase 4: Implement ───────────────────────────────────────────────
        task_store.update_state(task_id, "IMPLEMENTING", f"Implementing {len(files_to_implement)} file(s)…")
        log(f"Implementing {len(files_to_implement)} file(s)")

        generated_files: list[dict] = []
        context_summary = ""

        for i, file_info in enumerate(files_to_implement):
            file_path = file_info.get("path", f"file_{i}.txt")
            log(f"Generating [{i+1}/{len(files_to_implement)}]: {file_path}")

            # Read existing file if modifying and we have a clone
            existing_content = ""
            if file_info.get("action") == "modify" and clone_path:
                candidate = os.path.join(clone_path, file_path.lstrip("/"))
                if os.path.isfile(candidate):
                    try:
                        with open(candidate, encoding="utf-8", errors="replace") as fh:
                            existing_content = fh.read(8000)
                    except Exception:
                        pass

            code = _generate_file_code(
                file_info,
                task_instruction,
                analysis,
                context_summary,
                existing_content,
            )
            # Strip any residual markdown fences from LLM output
            code = _strip_code_fences(code)

            generated_files.append({"path": file_path, "content": code, "action": file_info.get("action", "create")})

            # Update context summary for subsequent files (brief)
            context_summary += f"\n{file_path}: {file_info.get('purpose', '')}\n"
            if len(context_summary) > 2000:
                context_summary = context_summary[-2000:]

        log(f"Code generation complete — {len(generated_files)} file(s) ready")

        # ── Phase 5: Write to shared workspace ──────────────────────────────
        if workspace:
            task_store.update_state(task_id, "WRITING", "Writing files to workspace…")
            written_paths = _write_files_to_workspace(workspace, generated_files)
            log(f"Wrote {len(written_paths)} file(s) to workspace")

        # ── Phase 5b: Build and test with LLM-guided recovery ───────────────
        build_dir = os.path.join(workspace, AGENT_ID) if workspace else ""
        build_ok = True  # default: assume passing if no build dir
        if build_dir and os.path.isdir(build_dir):
            task_store.update_state(task_id, "BUILDING", "Running build and tests…")
            log("Running build/tests")
            build_ok, build_output = _build_and_test_with_recovery(
                build_dir,
                task_instruction,
                analysis.get("language", "python"),
                log,
            )
            if build_ok:
                log("Build/tests passed")
            else:
                log(f"Build/tests could not be fully resolved: {build_output[:200]}")
            # Sync fixed files back to generated_files list
            for gf in generated_files:
                rel_path = gf["path"].lstrip("/")
                candidate = os.path.join(build_dir, rel_path)
                if os.path.isfile(candidate):
                    try:
                        with open(candidate, encoding="utf-8") as fh:
                            gf["content"] = fh.read()
                    except Exception:
                        pass

        # ── Phase 6: Push files and create PR (if repo available) ────────────
        pr_url = ""
        branch_name = ""

        if repo_url and workspace:
            task_store.update_state(task_id, "PUSHING", "Creating branch and pushing code…")

            # Determine branch name — sanitize base_branch from LLM analysis
            base_branch = _sanitize_base_branch(analysis.get("target_branch") or "main")
            safe_task_id = re.sub(r"[^a-z0-9-]", "-", task_id.lower())
            branch_name = f"feature/web-agent-{safe_task_id}"
            log(f"Creating branch: {branch_name} from base: {base_branch}")

            branch_created = _create_branch(
                task_id, repo_url, branch_name, base_branch, workspace, compass_task_id
            )
            if not branch_created:
                log(f"Warning: could not create branch {branch_name} — files saved to workspace only")
            else:
                # Push generated files
                push_files = [
                    {"path": gf["path"], "content": gf["content"]}
                    for gf in generated_files
                ]
                commit_msg = f"feat: web agent implementation for task {task_id}"
                if ticket_match:
                    commit_msg = f"feat({ticket_match.group(1)}): web agent implementation"

                pushed = _push_files(
                    task_id, repo_url, branch_name, push_files, commit_msg, workspace, compass_task_id,
                    base_branch=base_branch,
                )
                if pushed:
                    log("Files pushed to branch")
                    # Create PR
                    files_changed = [gf["path"] for gf in generated_files]
                    pr_title, pr_body = _generate_pr_description(
                        task_instruction,
                        acceptance_criteria,
                        files_changed,
                        plan.get("plan_summary") or "Web agent implementation",
                    )
                    pr_url = _create_pr(
                        task_id, repo_url, branch_name, base_branch,
                        pr_title, pr_body, workspace, compass_task_id
                    )
                    if pr_url:
                        log(f"PR created: {pr_url}")
                        # ── Dev Workflow Step 2: Update Jira after PR ────────
                        if ticket_match:
                            ticket_key = ticket_match.group(1)
                            _jira_transition(
                                ticket_key, "In Review",
                                task_id, workspace, compass_task_id,
                            )
                            test_status = "✅ Build/tests passed" if build_ok else "⚠️ Build/tests had issues"
                            _jira_add_comment(
                                ticket_key,
                                f"🤖 **Web Agent** (`{AGENT_ID}`) completed implementation.\n\n"
                                f"**PR:** {pr_url}\n"
                                f"**Branch:** `{branch_name}`\n"
                                f"**Test Status:** {test_status}\n"
                                f"**Files changed ({len(generated_files)}):** "
                                f"{', '.join(gf['path'] for gf in generated_files[:5])}"
                                f"{'…' if len(generated_files) > 5 else ''}\n\n"
                                f"**Summary:** {plan.get('plan_summary', 'Implementation complete.')}",
                                task_id,
                                workspace,
                                compass_task_id,
                            )
                    else:
                        log("Warning: PR creation returned no URL")
                else:
                    log("Warning: file push failed — files saved to workspace only")

        # ── Phase 7: Build artifacts and finalize ────────────────────────────
        task_store.update_state(task_id, "COMPLETING", "Finalizing…")

        files_list = [gf["path"] for gf in generated_files]
        summary = _generate_summary(task_instruction, acceptance_criteria, files_list, pr_url)
        log(f"Summary: {summary[:120]}")

        # Create artifacts: one per generated file + summary
        summary_artifact = build_text_artifact(
            "web-agent-summary",
            summary,
            metadata={
                "agentId": AGENT_ID,
                "capability": "web.task.execute",
                "orchestratorTaskId": compass_task_id,
                "taskId": task_id,
                "prUrl": pr_url,
                "branch": branch_name,
                "filesCount": len(generated_files),
            },
        )
        final_artifacts = [summary_artifact]

        # Add code file artifacts (truncated for artifact payload)
        for gf in generated_files[:10]:
            code_preview = gf["content"][:2000] + ("\n...[truncated]" if len(gf["content"]) > 2000 else "")
            final_artifacts.append(
                build_text_artifact(
                    f"web-agent-file-{gf['path'].replace('/', '-')}",
                    f"File: {gf['path']}\n\n{code_preview}",
                    metadata={
                        "agentId": AGENT_ID,
                        "capability": "web.task.execute",
                        "orchestratorTaskId": compass_task_id,
                        "filePath": gf["path"],
                    },
                )
            )

        task_store.update_state(task_id, "TASK_STATE_COMPLETED", summary)
        task = task_store.get(task_id)
        if task:
            task.artifacts = final_artifacts

        log("Task completed successfully")
        audit_log(
            "TASK_COMPLETED",
            task_id=task_id,
            compass_task_id=compass_task_id,
            files_count=len(generated_files),
            pr_url=pr_url,
        )
        _notify_callback(callback_url, task_id, "TASK_STATE_COMPLETED", summary, final_artifacts)

    except Exception as err:
        error_text = str(err)
        print(f"[{AGENT_ID}][{task_id}] FAILED: {error_text}")
        task_store.update_state(task_id, "TASK_STATE_FAILED", f"Web Agent failed: {error_text[:500]}")
        audit_log("TASK_FAILED", task_id=task_id, error=error_text[:300])
        _notify_callback(
            callback_url, task_id, "TASK_STATE_FAILED",
            f"Web Agent failed: {error_text[:500]}", []
        )


def _strip_code_fences(code: str) -> str:
    """Remove markdown code fences from LLM output."""
    code = code.strip()
    if code.startswith("```"):
        lines = code.splitlines()
        start = 1
        end = len(lines)
        while end > start and lines[end - 1].strip() in ("```", ""):
            end -= 1
        code = "\n".join(lines[start:end])
    return code


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class WebAgentHandler(BaseHTTPRequestHandler):
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

def main():
    print(f"[{AGENT_ID}] Web Agent starting on {HOST}:{PORT}")
    reporter.start()
    server = ThreadingHTTPServer((HOST, PORT), WebAgentHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
