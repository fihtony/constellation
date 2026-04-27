"""SCM Agent — multi-provider Source Code Manager.

Supports GitHub and Bitbucket Server out of the box.
The active provider is selected via the SCM_PROVIDER environment variable.
Future providers (GitLab, etc.) can be added under scm/providers/.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from common.devlog import debug_log
from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.llm_client import generate_text
from common.message_utils import build_text_artifact, extract_text

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8020"))
AGENT_ID = os.environ.get("AGENT_ID", "scm-agent")
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{AGENT_ID}-0")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://scm:{PORT}")
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://compass:8080")

# Provider selection: "github" | "bitbucket"  (default: auto-detect from SCM_BASE_URL)
_SCM_PROVIDER = os.environ.get("SCM_PROVIDER", "").strip().lower()
_SCM_BASE_URL = os.environ.get("SCM_BASE_URL", "").strip()
_SCM_TOKEN = os.environ.get("SCM_TOKEN", "").strip()
_SCM_USERNAME = os.environ.get("SCM_USERNAME", "").strip()
_SCM_AUTH_MODE = os.environ.get("SCM_AUTH_MODE", "auto").strip().lower()
_SCM_DEFAULT_PROJECT = os.environ.get("SCM_DEFAULT_PROJECT", "").strip()
_CORP_CA_BUNDLE = os.environ.get("CORP_CA_BUNDLE", "") or os.environ.get("SSL_CERT_FILE", "")
_GIT_AUTHOR_NAME = os.environ.get("SCM_GIT_AUTHOR_NAME", "SCM Agent")
_GIT_AUTHOR_EMAIL = (
    os.environ.get("SCM_GIT_AUTHOR_EMAIL")
    or os.environ.get("JIRA_EMAIL")
    or "scm-agent@local"
)

CLONE_TIMEOUT_SECONDS = int(os.environ.get("CLONE_TIMEOUT_SECONDS", "600"))
_REPO_TREE_MAX_FILES = int(os.environ.get("REPO_TREE_MAX_FILES", "500"))
_REPO_FILE_MAX_BYTES = int(os.environ.get("REPO_FILE_MAX_BYTES", str(512 * 1024)))

# Back-end selector (only applies when SCM_PROVIDER=github): "rest" (default) | "mcp"
_SCM_BACKEND = os.environ.get("SCM_BACKEND", "rest").strip().lower()

# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def _make_provider():
    from scm.providers.github import GitHubProvider
    from scm.providers.github_mcp import GitHubMCPProvider
    from scm.providers.bitbucket import BitbucketProvider

    provider_name = _SCM_PROVIDER
    if not provider_name:
        # Auto-detect from base URL
        url_lower = _SCM_BASE_URL.lower()
        if "github.com" in url_lower or not url_lower:
            provider_name = "github"
        else:
            provider_name = "bitbucket"

    if provider_name == "github":
        if _SCM_BACKEND == "mcp":
            print(f"[{AGENT_ID}] GitHub back-end: MCP (remote HTTP)")
            return GitHubMCPProvider(
                token=_SCM_TOKEN,
                author_name=_GIT_AUTHOR_NAME,
                author_email=_GIT_AUTHOR_EMAIL,
            )
        print(f"[{AGENT_ID}] GitHub back-end: REST API")
        return GitHubProvider(
            token=_SCM_TOKEN,
            username=_SCM_USERNAME,
            author_name=_GIT_AUTHOR_NAME,
            author_email=_GIT_AUTHOR_EMAIL,
        )
    elif provider_name == "bitbucket":
        return BitbucketProvider(
            base_url=_SCM_BASE_URL,
            token=_SCM_TOKEN,
            username=_SCM_USERNAME,
            auth_mode=_SCM_AUTH_MODE,
            default_project=_SCM_DEFAULT_PROJECT,
            ca_bundle=_CORP_CA_BUNDLE,
            author_name=_GIT_AUTHOR_NAME,
            author_email=_GIT_AUTHOR_EMAIL,
        )
    else:
        raise ValueError(f"Unknown SCM_PROVIDER: {provider_name!r}. Must be 'github' or 'bitbucket'.")


_provider = _make_provider()

# ---------------------------------------------------------------------------
# Task state
# ---------------------------------------------------------------------------

TASK_SEQ = 0
TASKS: dict = {}
TASKS_LOCK = threading.Lock()
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _next_task_id() -> str:
    global TASK_SEQ
    TASK_SEQ += 1
    return f"scm-task-{TASK_SEQ:04d}"


def _create_task(initial_state: str, initial_message: str) -> str:
    task_id = _next_task_id()
    now = time.time()
    with TASKS_LOCK:
        TASKS[task_id] = {
            "id": task_id,
            "agentId": AGENT_ID,
            "state": initial_state,
            "message": initial_message,
            "artifacts": [],
            "extra": {},
            "createdAt": now,
            "updatedAt": now,
        }
    return task_id


def _update_task(task_id: str, state=None, message=None, artifacts=None, extra=None):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return
        if state is not None:
            task["state"] = state
        if message is not None:
            task["message"] = message
        if artifacts is not None:
            task["artifacts"] = artifacts
        if extra is not None:
            task["extra"].update(extra)
        task["updatedAt"] = time.time()


def _task_payload(task_id: str) -> dict:
    with TASKS_LOCK:
        task = TASKS.get(task_id, {})
        state = task.get("state", "TASK_STATE_UNKNOWN")
        return {
            "task": {
                "id": task_id,
                "agentId": task.get("agentId", AGENT_ID),
                "status": {
                    "state": state,
                    "message": {"role": "ROLE_AGENT", "parts": [{"text": task.get("message", "")}]},
                },
                "artifacts": task.get("artifacts", []),
                "extra": task.get("extra", {}),
            }
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_json(url: str, payload: dict, timeout: int = 10):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, json.loads(raw) if raw.strip() else {}


def _notify_completion(message: dict, task_id: str, state: str, status_text: str, artifacts: list):
    callback_url = (message.get("metadata") or {}).get("orchestratorCallbackUrl", "")
    if not callback_url:
        return
    try:
        _post_json(callback_url, {
            "downstreamTaskId": task_id,
            "state": state,
            "statusMessage": status_text,
            "artifacts": artifacts,
            "agentId": AGENT_ID,
        })
        print(f"[{AGENT_ID}] Callback sent → {callback_url}")
    except Exception as exc:
        print(f"[{AGENT_ID}] Callback failed: {exc}")


def _load_agent_card() -> dict:
    card_path = os.path.join(os.path.dirname(__file__), "agent-card.json")
    with open(card_path, encoding="utf-8") as fh:
        card = json.load(fh)
    text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
    return json.loads(text)


def _extract_owner_repo(text: str) -> tuple[str, str]:
    """Try to parse owner/repo from a GitHub URL or 'owner/repo' notation."""
    # Full GitHub URL: https://github.com/owner/repo
    m = re.search(r"github\.com/([^/\s]+)/([^/\s?#]+)", text or "")
    if m:
        repo = m.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return m.group(1), repo
    # Bitbucket project/repo
    m2 = re.search(r"/projects/([^/\s]+)/repos/([^/\s?#]+)", text or "")
    if m2:
        return m2.group(1), m2.group(2)
    return "", ""


def _read_skill_guide(limit: int = 2200) -> str:
    """Load workspace skill guide from .github/skills/ if present."""
    for candidate in [
        os.path.join(os.path.dirname(__file__), "..", ".github", "skills", "bitbucket-server-workflow", "SKILL.md"),
        os.path.join(os.path.dirname(__file__), "..", ".github", "skills", "scm-workflow", "SKILL.md"),
    ]:
        path = os.path.normpath(candidate)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            return content[:limit]
    return ""


# ---------------------------------------------------------------------------
# Git clone to shared workspace (async)
# ---------------------------------------------------------------------------

def _clone_to_workspace(
    owner: str, repo: str, branch: str, target_path: str
) -> tuple[str | None, str]:
    clone_url = _provider.get_clone_url(owner, repo)
    clone_dir = os.path.join(target_path, repo)
    os.makedirs(target_path, exist_ok=True)
    git_config = []
    token = _SCM_TOKEN
    if token:
        git_config.extend(["-c", f"http.extraHeader=Authorization: Bearer {token}"])
    if _CORP_CA_BUNDLE and os.path.isfile(_CORP_CA_BUNDLE):
        git_config.extend(["-c", f"http.sslCAInfo={_CORP_CA_BUNDLE}"])

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    if os.path.isdir(os.path.join(clone_dir, ".git")):
        r = subprocess.run(
            ["git", *git_config, "fetch", "--depth", "1", "origin", branch],
            cwd=clone_dir, capture_output=True, text=True,
            timeout=CLONE_TIMEOUT_SECONDS, env=env,
        )
        if r.returncode != 0:
            return None, f"fetch_failed: {(r.stdout or r.stderr)[:200]}"
        subprocess.run(["git", "checkout", branch], cwd=clone_dir, capture_output=True, env=env)
        return clone_dir, "fetched"

    r = subprocess.run(
        ["git", *git_config, "clone", "--depth", "1", "--branch", branch, clone_url, clone_dir],
        capture_output=True, text=True, timeout=CLONE_TIMEOUT_SECONDS, env=env,
    )
    if r.returncode != 0:
        return None, f"clone_failed: {(r.stdout or r.stderr)[:200]}"
    return clone_dir, "cloned"


def _repo_tree(clone_dir: str, max_depth: int = 4) -> str:
    root = Path(clone_dir).resolve()
    if not root.is_dir():
        return f"(directory not found: {clone_dir})"
    lines: list[str] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel = Path(dirpath).relative_to(root)
        depth = len(rel.parts)
        if depth >= max_depth:
            dirnames.clear()
            continue
        indent = "  " * depth
        if depth > 0:
            lines.append(f"{indent}{rel.parts[-1]}/")
        for fname in sorted(filenames):
            if count >= _REPO_TREE_MAX_FILES:
                lines.append(f"{indent}  ... (truncated)")
                return "\n".join(lines)
            lines.append(f"{indent}  {fname}")
            count += 1
    return "\n".join(lines) if lines else "(empty repository)"


def _repo_file(clone_dir: str, file_path: str) -> tuple[str, str]:
    rel = file_path.lstrip("/")
    root = Path(clone_dir).resolve()
    target = (root / rel).resolve()
    if root not in target.parents and target != root:
        return "", "unsafe_path"
    if not target.is_file():
        return "", "not_found"
    if target.stat().st_size > _REPO_FILE_MAX_BYTES:
        return "", "too_large"
    return target.read_text(encoding="utf-8", errors="replace"), "ok"


def _fire_clone_callback(callback_url: str, task_id: str, state: str, clone_dir: str, error: str):
    if not callback_url:
        return
    try:
        _post_json(callback_url, {
            "taskId": task_id,
            "agentId": AGENT_ID,
            "state": state,
            "clonePath": clone_dir or "",
            "error": error or "",
        })
    except Exception as exc:
        print(f"[{AGENT_ID}] Clone callback failed: {exc}")


def _clone_async_worker(task_id: str, owner: str, repo: str, branch: str, target_path: str, callback_url: str):
    try:
        _update_task(task_id, state="TASK_STATE_WORKING",
                     message=f"Cloning {owner}/{repo} branch={branch} …")
        clone_dir, result = _clone_to_workspace(owner, repo, branch, target_path)
        if clone_dir:
            _update_task(task_id, state="TASK_STATE_COMPLETED",
                         message=f"Cloned {owner}/{repo} → {clone_dir} ({result})",
                         extra={"clonePath": clone_dir, "result": result})
            _fire_clone_callback(callback_url, task_id, "TASK_STATE_COMPLETED", clone_dir, "")
        else:
            _update_task(task_id, state="TASK_STATE_FAILED",
                         message=f"Clone failed: {result}",
                         extra={"clonePath": "", "result": result})
            _fire_clone_callback(callback_url, task_id, "TASK_STATE_FAILED", "", result)
    except Exception as exc:
        _update_task(task_id, state="TASK_STATE_FAILED", message=str(exc))
        _fire_clone_callback(callback_url, task_id, "TASK_STATE_FAILED", "", str(exc))


# ---------------------------------------------------------------------------
# Core message processor
# ---------------------------------------------------------------------------

def process_message(message: dict) -> tuple[str, list]:
    """Route message to the correct SCM operation and return (status_text, artifacts)."""
    text = extract_text(message)
    metadata = message.get("metadata") or {}
    capability = metadata.get("requestedCapability", "")

    # Build LLM prompt context
    skill = _read_skill_guide()
    provider_note = f"Active SCM provider: {_provider.provider_name}"
    system_prompt = "\n\n".join(filter(None, [
        "You are an SCM agent. Analyze the request and extract structured parameters.",
        provider_note,
        skill,
    ]))

    print(f"[{AGENT_ID}] processing capability={capability!r} text={text[:120]!r}")

    # Route by requested capability
    if capability == "scm.repo.search":
        return _handle_repo_search(text)
    if capability in ("scm.repo.inspect", "scm.repo.resolve"):
        return _handle_repo_inspect(text)
    if capability == "scm.branch.create":
        return _handle_branch_create(text, message)
    if capability == "scm.branch.list":
        return _handle_branch_list(text)
    if capability == "scm.pr.create":
        return _handle_pr_create(text, message)
    if capability in ("scm.pr.get", "scm.pr.inspect"):
        return _handle_pr_get(text)
    if capability == "scm.pr.list":
        return _handle_pr_list(text)
    if capability == "scm.pr.comment":
        return _handle_pr_comment(text, message)
    if capability == "scm.pr.comment.list":
        return _handle_pr_comment_list(text)
    if capability == "scm.git.push":
        return _handle_git_push(text, message)

    # Default: use LLM to infer intent and dispatch
    return _handle_llm_dispatch(text, message, system_prompt)


# ---------------------------------------------------------------------------
# Capability handlers
# ---------------------------------------------------------------------------

def _parse_owner_repo(text: str) -> tuple[str, str]:
    owner, repo = _extract_owner_repo(text)
    if not owner or not repo:
        # Try plain 'owner/repo' syntax
        m = re.search(r"\b([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)\b", text)
        if m:
            owner, repo = m.group(1), m.group(2)
    return owner, repo


def _handle_repo_search(text: str) -> tuple[str, list]:
    repos, status = _provider.search_repos(text, limit=10)
    if status != "ok":
        return f"Repository search failed: {status}", []
    summary = "\n".join(
        f"- {r['fullName']}: {r.get('description', '')} [{r.get('htmlUrl', '')}]"
        for r in repos
    )
    artifact = build_text_artifact(
        "repo-search-results",
        summary or "No repositories found.",
        metadata={"agentId": AGENT_ID, "capability": "scm.repo.search"},
    )
    return f"Found {len(repos)} repositories.", [artifact]


def _handle_repo_inspect(text: str) -> tuple[str, list]:
    owner, repo = _parse_owner_repo(text)
    if not owner or not repo:
        return "Could not parse owner/repo from request.", []
    info, status = _provider.get_repo(owner, repo)
    if status != "ok":
        return f"Repository lookup failed: {status}", []
    branches, _ = _provider.list_branches(owner, repo)
    result = {**info, "branches": branches[:20]}
    artifact = build_text_artifact(
        "repo-info",
        json.dumps(result, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.repo.inspect"},
    )
    return f"Repository {owner}/{repo} fetched ({len(branches)} branches).", [artifact]


def _handle_branch_list(text: str) -> tuple[str, list]:
    owner, repo = _parse_owner_repo(text)
    if not owner or not repo:
        return "Could not parse owner/repo from request.", []
    branches, status = _provider.list_branches(owner, repo)
    if status != "ok":
        return f"Branch list failed: {status}", []
    artifact = build_text_artifact(
        "branches",
        json.dumps(branches, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.branch.list"},
    )
    return f"Listed {len(branches)} branches for {owner}/{repo}.", [artifact]


def _handle_branch_create(text: str, message: dict) -> tuple[str, list]:
    owner, repo = _parse_owner_repo(text)
    branch_m = re.search(r"branch[:\s]+([^\s,]+)", text, re.IGNORECASE)
    from_m = re.search(r"from[:\s]+([^\s,]+)", text, re.IGNORECASE)
    branch = branch_m.group(1) if branch_m else ""
    from_ref = from_m.group(1) if from_m else "main"
    if not owner or not repo or not branch:
        return "Could not parse owner/repo/branch from request.", []
    result, status = _provider.create_branch(owner, repo, branch, from_ref)
    if status not in ("created",):
        return f"Branch creation failed: {status} — {result}", []
    artifact = build_text_artifact(
        "branch-created",
        json.dumps(result, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.branch.create"},
    )
    return f"Branch '{branch}' created in {owner}/{repo} from '{from_ref}'.", [artifact]


def _handle_pr_list(text: str) -> tuple[str, list]:
    owner, repo = _parse_owner_repo(text)
    state_m = re.search(r"\b(open|closed|all|merged)\b", text, re.IGNORECASE)
    state = state_m.group(1).lower() if state_m else "open"
    if not owner or not repo:
        return "Could not parse owner/repo from request.", []
    prs, status = _provider.list_prs(owner, repo, state)
    if status != "ok":
        return f"PR list failed: {status}", []
    artifact = build_text_artifact(
        "pull-requests",
        json.dumps(prs, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.pr.list"},
    )
    return f"Listed {len(prs)} {state} PRs for {owner}/{repo}.", [artifact]


def _handle_pr_get(text: str) -> tuple[str, list]:
    owner, repo = _parse_owner_repo(text)
    pr_m = re.search(r"(?:pr|pull.request|#)\s*(\d+)", text, re.IGNORECASE)
    pr_id = int(pr_m.group(1)) if pr_m else 0
    if not owner or not repo or not pr_id:
        return "Could not parse owner/repo/PR number from request.", []
    pr, status = _provider.get_pr(owner, repo, pr_id)
    if status != "ok":
        return f"PR fetch failed: {status}", []
    artifact = build_text_artifact(
        f"pr-{pr_id}",
        json.dumps(pr, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.pr.get",
                  "linkedJiraIssues": pr.get("linkedJiraIssues", [])},
    )
    return f"PR #{pr_id} fetched: '{pr.get('title', '')}'", [artifact]


def _handle_pr_create(text: str, message: dict) -> tuple[str, list]:
    # Prefer structured prPayload from metadata (avoids brittle text parsing)
    pr_payload = (message.get("metadata") or {}).get("prPayload") or {}
    if pr_payload:
        owner = pr_payload.get("owner", "")
        repo = pr_payload.get("repo", "")
        from_branch = pr_payload.get("fromBranch", "")
        to_branch = pr_payload.get("toBranch", "main")
        title = pr_payload.get("title", f"PR from {from_branch}")
        description = pr_payload.get("description", "")
    else:
        # Fall back to text parsing
        owner, repo = _parse_owner_repo(text)
        from_m = re.search(r"from[:\s]+([^\s,]+)", text, re.IGNORECASE)
        to_m = re.search(r"(?:to|into|target)[:\s]+([^\s,]+)", text, re.IGNORECASE)
        title_m = re.search(r"title[:\s]+(.+)", text, re.IGNORECASE)
        from_branch = from_m.group(1) if from_m else ""
        to_branch = to_m.group(1) if to_m else "main"
        title = title_m.group(1).strip() if title_m else f"PR from {from_branch}"
        description = ""
        # Reject to_branch values that look like Jira keys (e.g. CSTL-1/feature)
        if re.match(r"^[A-Z][A-Z0-9]+-\d+", to_branch or ""):
            to_branch = "main"
    if not owner or not repo or not from_branch:
        return "Could not parse owner/repo/from_branch from request.", []
    pr, status = _provider.create_pr(owner, repo, from_branch, to_branch, title, description)
    if status not in ("created",):
        return f"PR creation failed: {status} — {pr}", []
    artifact = build_text_artifact(
        "pr-created",
        json.dumps(pr, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.pr.create"},
    )
    return f"PR created: '{title}' ({pr.get('htmlUrl', '')})", [artifact]


def _handle_pr_comment(text: str, message: dict) -> tuple[str, list]:
    owner, repo = _parse_owner_repo(text)
    pr_m = re.search(r"(?:pr|pull.request|#)\s*(\d+)", text, re.IGNORECASE)
    pr_id = int(pr_m.group(1)) if pr_m else 0
    comment_m = re.search(r"comment[:\s]+(.+)", text, re.IGNORECASE | re.DOTALL)
    comment_text = comment_m.group(1).strip() if comment_m else text
    if not owner or not repo or not pr_id:
        return "Could not parse owner/repo/PR number from request.", []
    result, status = _provider.add_pr_comment(owner, repo, pr_id, comment_text)
    if status not in ("created",):
        return f"PR comment failed: {status} — {result}", []
    return f"Comment added to PR #{pr_id} in {owner}/{repo}.", [
        build_text_artifact(
            "pr-comment",
            json.dumps(result, ensure_ascii=False, indent=2),
            metadata={"agentId": AGENT_ID, "capability": "scm.pr.comment"},
        )
    ]


def _handle_pr_comment_list(text: str) -> tuple[str, list]:
    owner, repo = _parse_owner_repo(text)
    pr_m = re.search(r"(?:pr|pull.request|#)\s*(\d+)", text, re.IGNORECASE)
    pr_id = int(pr_m.group(1)) if pr_m else 0
    if not owner or not repo or not pr_id:
        return "Could not parse owner/repo/PR number from request.", []
    comments, status = _provider.list_pr_comments(owner, repo, pr_id)
    if status != "ok":
        return f"PR comment list failed: {status}", []
    return f"Listed {len(comments)} comments on PR #{pr_id}.", [
        build_text_artifact(
            "pr-comments",
            json.dumps(comments, ensure_ascii=False, indent=2),
            metadata={"agentId": AGENT_ID, "capability": "scm.pr.comment.list"},
        )
    ]


def _handle_git_push(text: str, message: dict) -> tuple[str, list]:
    # Prefer structured pushPayload from metadata over text parsing
    payload = (message.get("metadata") or {}).get("pushPayload") or {}
    if payload:
        owner = payload.get("owner", "")
        repo = payload.get("repo", "")
    else:
        owner, repo = _parse_owner_repo(text)
    branch = payload.get("branch", "")
    base_branch = payload.get("baseBranch", "main")
    files = payload.get("files", [])
    commit_msg = payload.get("commitMessage", "SCM agent commit")
    files_to_delete = payload.get("filesToDelete", [])
    if not owner or not repo or not branch or not files:
        return "Missing owner/repo/branch/files for git push.", []
    result, status = _provider.push_files(owner, repo, branch, base_branch, files, commit_msg, files_to_delete)
    if status not in ("pushed",):
        return f"Git push failed: {status} — {result}", []
    return f"Pushed {len(files)} file(s) to {owner}/{repo}:{branch}.", [
        build_text_artifact(
            "git-push",
            json.dumps(result, ensure_ascii=False, indent=2),
            metadata={"agentId": AGENT_ID, "capability": "scm.git.push"},
        )
    ]


def _handle_llm_dispatch(text: str, message: dict, system_prompt: str) -> tuple[str, list]:
    """Use LLM to understand intent and call the appropriate provider method."""
    try:
        llm_response = generate_text(
            system=system_prompt,
            prompt=text,
            max_tokens=1024,
        )
    except Exception as exc:
        llm_response = f"LLM error: {exc}"

    artifact = build_text_artifact(
        "scm-analysis",
        llm_response,
        metadata={"agentId": AGENT_ID, "provider": _provider.provider_name},
    )
    return llm_response[:200], [artifact]


# ---------------------------------------------------------------------------
# Async task runner
# ---------------------------------------------------------------------------

def _run_task_async(task_id: str, message: dict):
    try:
        _update_task(task_id, state="TASK_STATE_WORKING",
                     message="SCM agent is processing the task.")
        # Special handling for async clone
        capability = (message.get("metadata") or {}).get("requestedCapability", "")
        if capability == "scm.git.clone":
            _dispatch_clone(task_id, message)
            return

        status_text, artifacts = process_message(message)
        _update_task(task_id, state="TASK_STATE_COMPLETED",
                     message=status_text, artifacts=artifacts)
        _notify_completion(message, task_id, "TASK_STATE_COMPLETED", status_text, artifacts)
    except Exception as error:
        print(f"[{AGENT_ID}] Task {task_id} failed: {error}")
        failure_text = f"SCM agent failed: {error}"
        _update_task(task_id, state="TASK_STATE_FAILED", message=failure_text, artifacts=[])
        _notify_completion(message, task_id, "TASK_STATE_FAILED", failure_text, [])


def _dispatch_clone(task_id: str, message: dict):
    """Kick off async git clone."""
    text = extract_text(message)
    metadata = message.get("metadata") or {}
    owner, repo = _parse_owner_repo(text)
    branch = re.search(r"branch[:\s]+([^\s,]+)", text, re.IGNORECASE)
    branch_name = branch.group(1) if branch else "main"
    target = metadata.get("sharedWorkspacePath") or tempfile.mkdtemp(prefix="scm-workspace-")
    callback_url = metadata.get("orchestratorCallbackUrl", "")
    if not owner or not repo:
        _update_task(task_id, state="TASK_STATE_FAILED",
                     message="Could not parse owner/repo for clone.")
        return
    # Run the actual clone in a background thread
    t = threading.Thread(
        target=_clone_async_worker,
        args=(task_id, owner, repo, branch_name, target, callback_url),
        daemon=True,
    )
    t.start()


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Suppress health-checks and agent-card polls; print everything else
        line = args[0] if args else ""
        if any(p in line for p in ("/health", "/.well-known/agent-card.json")):
            return
        print(
            f"[{AGENT_ID}] {line} "
            f"{args[1] if len(args) > 1 else ''} "
            f"{args[2] if len(args) > 2 else ''}"
        )

    def _send_json(self, code: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": AGENT_ID,
                                  "provider": _provider.provider_name})
            return

        if path == "/.well-known/agent-card.json":
            self._send_json(200, _load_agent_card())
            return

        if path.startswith("/tasks/"):
            task_id = path[len("/tasks/"):]
            self._send_json(200, _task_payload(task_id))
            return

        # Read-only SCM endpoints (GET convenience API)
        if path == "/scm/repo":
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            if not owner or not repo:
                self._send_json(400, {"error": "missing owner/repo"})
                return
            info, status = _provider.get_repo(owner, repo)
            branches, _ = _provider.list_branches(owner, repo)
            result = {**info, "branches": branches[:20]}
            self._send_json(200 if status == "ok" else 404, {"repo": result, "status": status})
            return

        if path == "/scm/branches":
            owner = qs.get("owner", [""])[0]
            repo = qs.get("repo", [""])[0]
            project = qs.get("project", [owner])[0]  # Bitbucket compat
            effective_owner = owner or project
            if not effective_owner or not repo:
                self._send_json(400, {"error": "missing owner/repo"})
                return
            branches, status = _provider.list_branches(effective_owner, repo)
            self._send_json(200 if status == "ok" else 500, {"branches": branches, "status": status})
            return

        if path == "/scm/pull-requests":
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            state = qs.get("state", ["open"])[0]
            prs, status = _provider.list_prs(owner, repo, state)
            self._send_json(200 if status == "ok" else 500, {"pullRequests": prs, "status": status})
            return

        if re.match(r"^/scm/pull-requests/\d+/comments$", path):
            pr_id = int(path.split("/")[3])
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            comments, status = _provider.list_pr_comments(owner, repo, pr_id)
            self._send_json(200 if status == "ok" else 500, {"comments": comments, "status": status})
            return

        if re.match(r"^/scm/pull-requests/\d+$", path):
            pr_id = int(path.rsplit("/", 1)[-1])
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            pr, status = _provider.get_pr(owner, repo, pr_id)
            self._send_json(200 if status == "ok" else 404, {"pr": pr, "status": status})
            return

        if path == "/scm/repo/tree":
            clone_path = qs.get("path", [""])[0]
            depth = int(qs.get("depth", ["4"])[0])
            if not clone_path or not os.path.isdir(clone_path):
                self._send_json(400, {"error": "invalid clone path"})
                return
            tree = _repo_tree(clone_path, max_depth=depth)
            self._send_json(200, {"tree": tree})
            return

        if path == "/scm/repo/file":
            clone_path = qs.get("path", [""])[0]
            file_path = qs.get("file", [""])[0]
            if not clone_path or not file_path:
                self._send_json(400, {"error": "missing path or file"})
                return
            content, status = _repo_file(clone_path, file_path)
            if status != "ok":
                self._send_json(404, {"error": status})
                return
            self._send_json(200, {"content": content, "file": file_path})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/message:send":
            body = self._read_body()
            message = body.get("message", {})
            task_id = _create_task("TASK_STATE_WORKING", "Task received.")
            print(f"[{AGENT_ID}] Task {task_id} received")
            t = threading.Thread(target=_run_task_async, args=(task_id, message), daemon=True)
            t.start()
            self._send_json(200, _task_payload(task_id))
            return

        # Direct SCM action endpoints
        if path == "/scm/branches":
            body = self._read_body()
            owner = body.get("owner") or body.get("project", "")
            repo = body.get("repo", "")
            branch = body.get("branch", "")
            from_ref = body.get("from_branch") or body.get("startPoint", "main")
            if not owner or not repo or not branch:
                self._send_json(400, {"error": "missing owner/repo/branch"})
                return
            result, status = _provider.create_branch(owner, repo, branch, from_ref)
            self._send_json(201 if status == "created" else 400, {"result": result, "status": status})
            return

        if path == "/scm/pull-requests":
            body = self._read_body()
            owner = body.get("owner") or body.get("project", "")
            repo = body.get("repo", "")
            from_branch = body.get("from_branch", "")
            to_branch = body.get("to_branch", "main")
            title = body.get("title", "")
            description = body.get("description", "")
            if not owner or not repo or not from_branch or not title:
                self._send_json(400, {"error": "missing owner/repo/from_branch/title"})
                return
            pr, status = _provider.create_pr(owner, repo, from_branch, to_branch, title, description)
            self._send_json(201 if status == "created" else 400, {"pr": pr, "status": status})
            return

        if path == "/scm/pull-requests/comments":
            body = self._read_body()
            owner = body.get("owner") or body.get("project", "")
            repo = body.get("repo", "")
            pr_id = body.get("prId") or body.get("pullRequestId", 0)
            text = body.get("text", "")
            file_path = body.get("filePath", "")
            line = body.get("line")
            if not owner or not repo or not pr_id or not text:
                self._send_json(400, {"error": "missing owner/repo/prId/text"})
                return
            result, status = _provider.add_pr_comment(owner, repo, pr_id, text, file_path, line)
            self._send_json(201 if status == "created" else 400, {"result": result, "status": status})
            return

        if path == "/scm/git/clone":
            body = self._read_body()
            owner = body.get("owner") or body.get("project", "")
            repo = body.get("repo", "")
            branch = body.get("branch", "main")
            target_path = body.get("targetPath", "")
            callback_url = body.get("callbackUrl", "")
            if not owner or not repo or not target_path:
                self._send_json(400, {"error": "missing owner/repo/targetPath"})
                return
            task_id = _create_task("TASK_STATE_WORKING", f"Cloning {owner}/{repo} …")
            t = threading.Thread(
                target=_clone_async_worker,
                args=(task_id, owner, repo, branch, target_path, callback_url),
                daemon=True,
            )
            t.start()
            self._send_json(202, {"taskId": task_id, "executionMode": "async"})
            return

        if path == "/scm/git/push":
            body = self._read_body()
            owner = body.get("owner") or body.get("project", "")
            repo = body.get("repo", "")
            branch = body.get("branch", "")
            base_branch = body.get("baseBranch", "main")
            files = body.get("files", [])
            commit_msg = body.get("commitMessage", "SCM agent commit")
            files_to_delete = body.get("filesToDelete", [])
            if not owner or not repo or not branch or not files:
                self._send_json(400, {"error": "missing owner/repo/branch/files"})
                return
            result, status = _provider.push_files(owner, repo, branch, base_branch, files, commit_msg, files_to_delete)
            self._send_json(200 if status == "pushed" else 500, {"result": result, "status": status})
            return

        self._send_json(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    reporter = InstanceReporter(
        agent_id=AGENT_ID,
        service_url=ADVERTISED_URL,
        port=PORT,
    )
    reporter.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[{AGENT_ID}] Listening on {HOST}:{PORT} (provider={_provider.provider_name})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        reporter.stop()
        server.server_close()


if __name__ == "__main__":
    main()
