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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.llm_client import generate_text
from common.message_utils import artifact_text, build_text_artifact, extract_text
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
JIRA_AGENT_URL = os.environ.get("JIRA_AGENT_URL", "http://tracker:8010")
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
    response = generate_text(prompt, f"[{AGENT_ID}] analyze", system_prompt=prompts.ANALYZE_SYSTEM)
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
    response = generate_text(prompt, f"[{AGENT_ID}] plan", system_prompt=prompts.PLAN_SYSTEM)
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
    return generate_text(prompt, f"[{AGENT_ID}] codegen:{file_info.get('path', '')}", system_prompt=prompts.CODEGEN_SYSTEM)


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
    """Fetch Jira ticket content via Tracker Agent."""
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
) -> bool:
    """Push generated files to feature branch via SCM Agent."""
    try:
        files_payload = json.dumps(files, ensure_ascii=False)
        message_text = (
            f"Push files to branch {branch_name} in {repo_url}.\n"
            f"Commit message: {commit_message}\n"
            f"Files: {files_payload}"
        )
        result = _call_sync_agent(
            SCM_AGENT_URL,
            "scm.git.push",
            message_text,
            task_id,
            workspace,
            compass_task_id,
        )
        state = result.get("status", {}).get("state", "")
        return state in ("TASK_STATE_COMPLETED", "COMPLETED")
    except Exception as err:
        print(f"[{AGENT_ID}] Could not push files to {branch_name}: {err}")
        return False


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
    """Create a pull request via SCM Agent. Returns PR URL."""
    try:
        message_text = (
            f"Create pull request from {branch_name} to {base_branch} in {repo_url}.\n"
            f"Title: {pr_title}\n"
            f"Body: {pr_body}"
        )
        result = _call_sync_agent(
            SCM_AGENT_URL,
            "scm.pr.create",
            message_text,
            task_id,
            workspace,
            compass_task_id,
        )
        for art in result.get("artifacts", []):
            text = artifact_text(art)
            if text:
                try:
                    data = json.loads(text)
                    return (
                        data.get("pr_url") or data.get("prUrl")
                        or data.get("html_url") or data.get("url") or ""
                    )
                except Exception:
                    url_match = re.search(r"https?://\S+", text)
                    if url_match:
                        return url_match.group()
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

        # ── Phase 6: Push files and create PR (if repo available) ────────────
        pr_url = ""
        branch_name = ""

        if repo_url and workspace:
            task_store.update_state(task_id, "PUSHING", "Creating branch and pushing code…")

            # Determine branch name
            base_branch = analysis.get("target_branch") or "main"
            safe_task_id = re.sub(r"[^a-z0-9-]", "-", task_id.lower())
            branch_name = f"feature/web-agent-{safe_task_id}"
            log(f"Creating branch: {branch_name}")

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
                    task_id, repo_url, branch_name, push_files, commit_msg, workspace, compass_task_id
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
