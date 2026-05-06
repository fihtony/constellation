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
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from common.devlog import debug_log, record_workspace_stage
from common.env_utils import build_isolated_git_env, load_dotenv
from common.instance_reporter import InstanceReporter
from common.message_utils import build_text_artifact, extract_text
from common.tools.control_tools import configure_control_tools
from common.rules_loader import build_system_prompt, load_rules
from common.prompt_builder import build_system_prompt_from_manifest
from common.agent_system_prompt import build_agent_system_prompt as _build_manifest_prompt
from common.runtime.adapter import get_runtime, require_agentic_runtime, summarize_runtime_configuration
from common.task_permissions import (
    PermissionDeniedError,
    audit_permission_check,
    build_permission_denied_artifact,
    build_permission_denied_details,
    parse_permission_grant,
    write_operation_audit,
    read_operation_audit,
)
from scm import prompts

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
# ORCHESTRATOR_URL: only used as a last-resort fallback for legacy callers.
# All A2A callbacks must use orchestratorCallbackUrl from message.metadata instead.
_LEGACY_ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "").strip()

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


def _run_agentic(
    prompt: str,
    actor: str,
    *,
    system_prompt: str | None = None,
    context: dict | None = None,
    timeout: int = 120,
    max_tokens: int = 4096,
) -> str:
    require_agentic_runtime("SCM Agent")
    result = get_runtime().run(
        prompt=prompt,
        context=context,
        system_prompt=system_prompt,
        timeout=timeout,
        max_tokens=max_tokens,
    )
    for warning in result.get("warnings") or []:
        print(f"[{AGENT_ID}] Runtime warning ({actor}): {warning}")
    return result.get("raw_response") or result.get("summary") or ""

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
    # Bitbucket personal repo browse URL
    m3 = re.search(r"/users/([^/\s]+)/repos/([^/\s?#]+)", text or "")
    if m3:
        return f"~{m3.group(1)}", m3.group(2)
    # Bitbucket clone URL
    m4 = re.search(r"/scm/([^/\s]+)/([^/\s?#]+?)(?:\.git)?(?=[\s/?#]|$)", text or "")
    if m4:
        return m4.group(1), m4.group(2)
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

def _resolve_clone_auth(owner: str, repo: str, clone_url: str) -> tuple[str, list[str]]:
    git_config: list[str] = ["-c", "credential.helper="]
    token = _SCM_TOKEN
    if token and "github.com/" in clone_url:
        clone_url = re.sub(r"https://[^@/]+@github\.com/", "https://github.com/", clone_url)
        basic_auth = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        git_config.extend(["-c", f"http.extraHeader=AUTHORIZATION: basic {basic_auth}"])
        token = ""
    if token and "x-access-token:" not in clone_url:
        git_config.extend(["-c", f"http.extraHeader=Authorization: Bearer {token}"])
    if _CORP_CA_BUNDLE and os.path.isfile(_CORP_CA_BUNDLE):
        git_config.extend(["-c", f"http.sslCAInfo={_CORP_CA_BUNDLE}"])
    return clone_url, git_config


def _runtime_config_summary() -> dict:
    return {
        "runtime": summarize_runtime_configuration(),
        "rulesLoaded": bool(load_rules("scm")),
        "workflowRulesLoaded": bool(load_rules("scm", include_workflow=True)),
        "provider": _provider.provider_name,
        "backend": _SCM_BACKEND,
    }


def _permission_enforcement_mode() -> str:
    return os.environ.get("PERMISSION_ENFORCEMENT", "strict").strip().lower() or "strict"


def _request_permissions(payload_permissions: dict | None = None, headers=None) -> tuple[dict | None, str]:
    if payload_permissions is not None:
        return payload_permissions, ""
    raw = ((headers or {}).get("X-Task-Permissions") or "").strip() if headers else ""
    if not raw:
        return None, "No permissions attached to request. Explicit permission grant required."
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        return None, "Invalid X-Task-Permissions header. Explicit permission grant required."


def _check_scm_permission(
    *,
    action: str,
    target: str,
    scope: str = "*",
    message: dict | None = None,
    payload_permissions: dict | None = None,
    headers=None,
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
    permissions_data, missing_reason = _request_permissions(
        payload_permissions if payload_permissions is not None else metadata.get("permissions"),
        headers=headers,
    )
    grant = parse_permission_grant(permissions_data)
    if grant:
        allowed, reason = grant.check("scm", action, scope)
        escalation = grant.escalation_for("scm", action, scope)
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


def _require_scm_permission(
    *,
    action: str,
    target: str,
    scope: str = "*",
    message: dict | None = None,
    payload_permissions: dict | None = None,
    headers=None,
) -> None:
    allowed, reason, escalation = _check_scm_permission(
        action=action,
        target=target,
        scope=scope,
        message=message,
        payload_permissions=payload_permissions,
        headers=headers,
    )
    if allowed:
        return
    if _permission_enforcement_mode() == "strict":
        metadata = (message or {}).get("metadata") or {}
        raise PermissionDeniedError(
            build_permission_denied_details(
                permission_agent="scm",
                target_agent=AGENT_ID,
                action=action,
                target=target,
                reason=reason,
                escalation=escalation or "require_user_approval",
                scope=scope,
                request_agent=str(metadata.get("requestAgent") or "").strip(),
                task_id=str(metadata.get("taskId") or ""),
                orchestrator_task_id=str(metadata.get("orchestratorTaskId") or ""),
            )
        )
    print(f"[{AGENT_ID}] WARN: permission check failed but enforcement={_permission_enforcement_mode()}: {reason}")


def _enforce_http_scm_permission(
    handler: BaseHTTPRequestHandler,
    *,
    action: str,
    target: str,
    scope: str = "*",
    payload_permissions: dict | None = None,
) -> bool:
    allowed, reason, escalation = _check_scm_permission(
        action=action,
        target=target,
        scope=scope,
        payload_permissions=payload_permissions,
        headers=handler.headers,
    )
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


def _clone_to_workspace(
    owner: str, repo: str, branch: str, target_path: str, *, depth: int = 1, full_history: bool = False
) -> tuple[str | None, str]:
    clone_url = _provider.get_clone_url(owner, repo)
    clone_dir = os.path.join(target_path, repo)
    os.makedirs(target_path, exist_ok=True)
    clone_url, git_config = _resolve_clone_auth(owner, repo, clone_url)

    env = build_isolated_git_env(scope=f"{AGENT_ID}-workspace-clone")

    depth_args = [] if full_history else ["--depth", str(max(1, depth))]

    if os.path.isdir(os.path.join(clone_dir, ".git")):
        fetch_cmd = ["git", *git_config, "fetch", "origin", branch]
        if not full_history:
            fetch_cmd.extend(["--depth", str(max(1, depth))])
        r = subprocess.run(
            fetch_cmd,
            cwd=clone_dir, capture_output=True, text=True,
            timeout=CLONE_TIMEOUT_SECONDS, env=env,
        )
        if r.returncode != 0:
            return None, f"fetch_failed: {(r.stdout or r.stderr)[:200]}"
        subprocess.run(["git", "checkout", branch], cwd=clone_dir, capture_output=True, env=env)
        return clone_dir, "fetched"

    r = subprocess.run(
        ["git", *git_config, "clone", *depth_args, "--branch", branch, clone_url, clone_dir],
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


def _clone_async_worker(task_id: str, owner: str, repo: str, branch: str, target_path: str, callback_url: str, *, depth: int = 1, full_history: bool = False):
    try:
        _update_task(task_id, state="TASK_STATE_WORKING",
                     message=f"Cloning {owner}/{repo} branch={branch} …")
        record_workspace_stage(
            target_path,
            "scm",
            f"Cloning {owner}/{repo} ({branch})",
            task_id=task_id,
            extra={
                "owner": owner,
                "repo": repo,
                "branch": branch,
                "runtimeConfig": _runtime_config_summary(),
            },
        )
        clone_dir, result = _clone_to_workspace(owner, repo, branch, target_path, depth=depth, full_history=full_history)
        if clone_dir:
            clone_artifact = {
                "name": "clone-result",
                "artifactType": "application/json",
                "parts": [{"text": json.dumps({"clonePath": clone_dir, "result": result})}],
                "metadata": {"agentId": AGENT_ID, "capability": "scm.git.clone"},
            }
            _update_task(task_id, state="TASK_STATE_COMPLETED",
                         message=f"Cloned {owner}/{repo} → {clone_dir} ({result})",
                         artifacts=[clone_artifact],
                         extra={"clonePath": clone_dir, "result": result})
            record_workspace_stage(
                target_path,
                "scm",
                f"Completed scm.git.clone for {owner}/{repo}",
                task_id=task_id,
                extra={
                    "clonePath": clone_dir,
                    "result": result,
                    "runtimeConfig": _runtime_config_summary(),
                },
            )
            _fire_clone_callback(callback_url, task_id, "TASK_STATE_COMPLETED", clone_dir, "")
        else:
            _update_task(task_id, state="TASK_STATE_FAILED",
                         message=f"Clone failed: {result}",
                         extra={"clonePath": "", "result": result})
            record_workspace_stage(
                target_path,
                "scm",
                f"Failed scm.git.clone for {owner}/{repo}",
                task_id=task_id,
                extra={
                    "clonePath": "",
                    "result": result,
                    "runtimeConfig": _runtime_config_summary(),
                },
            )
            _fire_clone_callback(callback_url, task_id, "TASK_STATE_FAILED", "", result)
    except Exception as exc:
        _update_task(task_id, state="TASK_STATE_FAILED", message=str(exc))
        record_workspace_stage(
            target_path,
            "scm",
            f"Failed scm.git.clone for {owner}/{repo}",
            task_id=task_id,
            extra={"error": str(exc), "runtimeConfig": _runtime_config_summary()},
        )
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
        _require_scm_permission(action="repo.search", target=text or "repo-search", message=message)
        return _handle_repo_search(text)
    if capability in ("scm.repo.inspect", "scm.repo.resolve"):
        owner, repo = _parse_owner_repo(text)
        _require_scm_permission(
            action="repo.inspect",
            target=f"{owner}/{repo}" if owner and repo else text or capability,
            message=message,
        )
        return _handle_repo_inspect(text)
    if capability == "scm.branch.create":
        return _handle_branch_create(text, message)
    if capability == "scm.branch.list":
        owner, repo = _parse_owner_repo(text)
        _require_scm_permission(
            action="branch.list",
            target=f"{owner}/{repo}" if owner and repo else text or capability,
            message=message,
        )
        return _handle_branch_list(text)
    if capability == "scm.pr.create":
        return _handle_pr_create(text, message)
    if capability in ("scm.pr.get", "scm.pr.inspect"):
        owner, repo = _parse_owner_repo(text)
        _require_scm_permission(
            action="pr.get",
            target=f"{owner}/{repo}" if owner and repo else text or capability,
            message=message,
        )
        return _handle_pr_get(text)
    if capability == "scm.pr.list":
        owner, repo = _parse_owner_repo(text)
        _require_scm_permission(
            action="pr.list",
            target=f"{owner}/{repo}" if owner and repo else text or capability,
            message=message,
        )
        return _handle_pr_list(text)
    if capability == "scm.pr.comment":
        return _handle_pr_comment(text, message)
    if capability == "scm.pr.comment.list":
        owner, repo = _parse_owner_repo(text)
        _require_scm_permission(
            action="pr.comment.list",
            target=f"{owner}/{repo}" if owner and repo else text or capability,
            message=message,
        )
        return _handle_pr_comment_list(text)
    if capability == "scm.git.push":
        return _handle_git_push(text, message)
    if capability == "scm.repo.read_file":
        return _handle_remote_read_file(text, message)
    if capability == "scm.repo.list_dir":
        return _handle_remote_list_dir(text, message)
    if capability == "scm.code.search":
        return _handle_code_search(text, message)
    if capability == "scm.ref.compare":
        return _handle_ref_compare(text, message)
    if capability == "scm.branch.default":
        return _handle_branch_default(text, message)
    if capability == "scm.branch.rules":
        return _handle_branch_rules(text, message)

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
    _require_scm_permission(
        action="branch.create",
        target=f"{owner}/{repo}:{branch}",
        scope=branch,
        message=message,
    )
    result, status = _provider.create_branch(owner, repo, branch, from_ref)
    _write_audit(
        message=message,
        operation="scm.branch.create",
        target={"owner": owner, "repo": repo, "branch": branch, "fromRef": from_ref},
        input_summary={"branch": branch, "fromRef": from_ref},
        result={"success": status == "created", "status": status},
    )
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
        # Reject to_branch values that look like Jira keys (e.g. PROJ-1/feature)
        if re.match(r"^[A-Z][A-Z0-9]+-\d+", to_branch or ""):
            to_branch = "main"
    if not owner or not repo or not from_branch:
        return "Could not parse owner/repo/from_branch from request.", []
    _require_scm_permission(
        action="pr.create",
        target=f"{owner}/{repo}:{from_branch}->{to_branch}",
        message=message,
    )
    pr, status = _provider.create_pr(owner, repo, from_branch, to_branch, title, description)
    _write_audit(
        message=message,
        operation="scm.pr.create",
        target={"owner": owner, "repo": repo, "fromBranch": from_branch, "toBranch": to_branch},
        input_summary={"title": title[:120]},
        result={"success": status in ("created", "already_exists"), "status": status,
                "prUrl": pr.get("htmlUrl", "") if isinstance(pr, dict) else ""},
    )
    if status not in ("created", "already_exists"):
        return f"PR creation failed: {status} — {pr}", []
    artifact = build_text_artifact(
        "pr-created",
        json.dumps(pr, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.pr.create"},
    )
    if status == "already_exists":
        return f"PR already exists: '{title}' ({pr.get('htmlUrl', '')})", [artifact]
    return f"PR created: '{title}' ({pr.get('htmlUrl', '')})", [artifact]


def _handle_pr_comment(text: str, message: dict) -> tuple[str, list]:
    owner, repo = _parse_owner_repo(text)
    pr_m = re.search(r"(?:pr|pull.request|#)\s*(\d+)", text, re.IGNORECASE)
    pr_id = int(pr_m.group(1)) if pr_m else 0
    comment_m = re.search(r"comment[:\s]+(.+)", text, re.IGNORECASE | re.DOTALL)
    comment_text = comment_m.group(1).strip() if comment_m else text
    if not owner or not repo or not pr_id:
        return "Could not parse owner/repo/PR number from request.", []
    _require_scm_permission(
        action="pr.comment",
        target=f"{owner}/{repo}#{pr_id}",
        scope="self",
        message=message,
    )
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
    _require_scm_permission(
        action="branch.push",
        target=f"{owner}/{repo}:{branch}",
        scope=branch,
        message=message,
    )
    result, status = _provider.push_files(owner, repo, branch, base_branch, files, commit_msg, files_to_delete)
    _write_audit(
        message=message,
        operation="scm.git.push",
        target={"owner": owner, "repo": repo, "branch": branch},
        input_summary={"filesCount": len(files), "commitMessage": commit_msg[:120]},
        result={"success": status == "pushed", "status": status},
    )
    if status not in ("pushed",):
        return f"Git push failed: {status} — {result}", []
    return f"Pushed {len(files)} file(s) to {owner}/{repo}:{branch}.", [
        build_text_artifact(
            "git-push",
            json.dumps(result, ensure_ascii=False, indent=2),
            metadata={"agentId": AGENT_ID, "capability": "scm.git.push"},
        )
    ]


# ---------------------------------------------------------------------------
# New remote read handlers (no local clone required)
# ---------------------------------------------------------------------------

def _handle_remote_read_file(text: str, message: dict) -> tuple[str, list]:
    metadata = (message.get("metadata") or {})
    payload = metadata.get("scmPayload") or {}
    owner = payload.get("owner", "") or ""
    repo = payload.get("repo", "") or ""
    path = payload.get("path", "") or ""
    ref = payload.get("ref", "") or ""
    if not owner or not repo:
        owner, repo = _parse_owner_repo(text)
    if not path:
        pm = re.search(r"(?:path|file)[:\s]+([^\s,]+)", text, re.IGNORECASE)
        path = pm.group(1) if pm else ""
    if not ref:
        rm = re.search(r"(?:ref|branch|at)[:\s]+([^\s,]+)", text, re.IGNORECASE)
        ref = rm.group(1) if rm else ""
    if not owner or not repo or not path:
        return "Missing owner/repo/path for remote file read.", []
    _require_scm_permission(
        action="repo.read_file", target=f"{owner}/{repo}:{path}", message=message
    )
    content, status = _provider.read_remote_file(owner, repo, path, ref)
    if status != "ok":
        return f"Remote file read failed: {status}", []
    artifact = build_text_artifact(
        "remote-file",
        content,
        metadata={"agentId": AGENT_ID, "capability": "scm.repo.read_file",
                  "owner": owner, "repo": repo, "path": path, "ref": ref},
    )
    return f"Read remote file {path} from {owner}/{repo} ref={ref or 'default'}.", [artifact]


def _handle_remote_list_dir(text: str, message: dict) -> tuple[str, list]:
    metadata = (message.get("metadata") or {})
    payload = metadata.get("scmPayload") or {}
    owner = payload.get("owner", "") or ""
    repo = payload.get("repo", "") or ""
    path = payload.get("path", "") or ""
    ref = payload.get("ref", "") or ""
    if not owner or not repo:
        owner, repo = _parse_owner_repo(text)
    if not ref:
        rm = re.search(r"(?:ref|branch|at)[:\s]+([^\s,]+)", text, re.IGNORECASE)
        ref = rm.group(1) if rm else ""
    if not owner or not repo:
        return "Missing owner/repo for remote dir list.", []
    _require_scm_permission(
        action="repo.list_dir", target=f"{owner}/{repo}:{path or '/'}", message=message
    )
    entries, status = _provider.list_remote_dir(owner, repo, path, ref)
    if status != "ok":
        return f"Remote dir list failed: {status}", []
    artifact = build_text_artifact(
        "remote-dir",
        json.dumps(entries, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.repo.list_dir",
                  "owner": owner, "repo": repo, "path": path, "ref": ref},
    )
    return f"Listed {len(entries)} entries in {owner}/{repo}:{path or '/'} ref={ref or 'default'}.", [artifact]


def _handle_code_search(text: str, message: dict) -> tuple[str, list]:
    metadata = (message.get("metadata") or {})
    payload = metadata.get("scmPayload") or {}
    owner = payload.get("owner", "") or ""
    repo = payload.get("repo", "") or ""
    query = payload.get("query", "") or text
    limit = int(payload.get("limit", 20))
    if not owner or not repo:
        owner, repo = _parse_owner_repo(text)
    if not owner or not repo:
        return "Missing owner/repo for code search.", []
    _require_scm_permission(
        action="code.search", target=f"{owner}/{repo}", message=message
    )
    results, status = _provider.search_code(owner, repo, query, limit)
    if status not in ("ok", "not_supported"):
        return f"Code search failed: {status}", []
    if status == "not_supported":
        return f"Code search not supported by {_provider.provider_name}.", []
    artifact = build_text_artifact(
        "code-search-results",
        json.dumps(results, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.code.search",
                  "owner": owner, "repo": repo, "query": query[:200]},
    )
    return f"Found {len(results)} code search result(s) in {owner}/{repo}.", [artifact]


def _handle_ref_compare(text: str, message: dict) -> tuple[str, list]:
    metadata = (message.get("metadata") or {})
    payload = metadata.get("scmPayload") or {}
    owner = payload.get("owner", "") or ""
    repo = payload.get("repo", "") or ""
    base = payload.get("base", "") or ""
    head = payload.get("head", "") or ""
    stat_only = bool(payload.get("statOnly", False))
    if not owner or not repo:
        owner, repo = _parse_owner_repo(text)
    if not base:
        bm = re.search(r"base[:\s]+([^\s,]+)", text, re.IGNORECASE)
        base = bm.group(1) if bm else "main"
    if not head:
        hm = re.search(r"(?:head|compare)[:\s]+([^\s,]+)", text, re.IGNORECASE)
        head = hm.group(1) if hm else ""
    if not owner or not repo or not head:
        return "Missing owner/repo/head for ref comparison.", []
    _require_scm_permission(
        action="ref.compare", target=f"{owner}/{repo}:{base}...{head}", message=message
    )
    result, status = _provider.compare_refs(owner, repo, base, head, stat_only=stat_only)
    if status != "ok":
        return f"Ref comparison failed: {status}", []
    artifact = build_text_artifact(
        "ref-comparison",
        json.dumps(result, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.ref.compare",
                  "owner": owner, "repo": repo, "base": base, "head": head},
    )
    ahead = result.get("aheadBy", "?")
    changed = result.get("totalChangedFiles", len(result.get("files", [])))
    return (
        f"Compared {owner}/{repo}: {head} is {ahead} commit(s) ahead of {base}, "
        f"{changed} file(s) changed."
    ), [artifact]


def _handle_branch_default(text: str, message: dict) -> tuple[str, list]:
    metadata = (message.get("metadata") or {})
    payload = metadata.get("scmPayload") or {}
    owner = payload.get("owner", "") or ""
    repo = payload.get("repo", "") or ""
    if not owner or not repo:
        owner, repo = _parse_owner_repo(text)
    if not owner or not repo:
        return "Missing owner/repo for default branch query.", []
    _require_scm_permission(
        action="branch.default", target=f"{owner}/{repo}", message=message
    )
    result, status = _provider.get_default_branch(owner, repo)
    if status != "ok":
        return f"Get default branch failed: {status}", []
    artifact = build_text_artifact(
        "default-branch",
        json.dumps(result, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.branch.default"},
    )
    return (
        f"Default branch of {owner}/{repo}: '{result.get('defaultBranch', '?')}', "
        f"{len(result.get('protectedBranches', []))} protected branch(es)."
    ), [artifact]


def _handle_branch_rules(text: str, message: dict) -> tuple[str, list]:
    metadata = (message.get("metadata") or {})
    payload = metadata.get("scmPayload") or {}
    owner = payload.get("owner", "") or ""
    repo = payload.get("repo", "") or ""
    if not owner or not repo:
        owner, repo = _parse_owner_repo(text)
    if not owner or not repo:
        return "Missing owner/repo for branch rules query.", []
    _require_scm_permission(
        action="branch.rules", target=f"{owner}/{repo}", message=message
    )
    result, status = _provider.get_branch_rules(owner, repo)
    if status != "ok":
        return f"Get branch rules failed: {status}", []
    artifact = build_text_artifact(
        "branch-rules",
        json.dumps(result, ensure_ascii=False, indent=2),
        metadata={"agentId": AGENT_ID, "capability": "scm.branch.rules"},
    )
    return (
        f"Branch rules for {owner}/{repo}: {len(result.get('rules', []))} rule(s) "
        f"from {result.get('source', 'unknown')}."
    ), [artifact]


# ---------------------------------------------------------------------------
# Operation-level audit helper
# ---------------------------------------------------------------------------

def _write_audit(
    *,
    message: dict,
    operation: str,
    target: dict,
    input_summary: dict,
    result: dict,
    duration_ms: int = 0,
) -> None:
    """Write a boundary-operation audit entry to the task workspace."""
    metadata = (message.get("metadata") or {})
    workspace_path = metadata.get("sharedWorkspacePath") or ""
    task_id = metadata.get("taskId") or metadata.get("orchestratorTaskId") or ""
    orchestrator_task_id = metadata.get("orchestratorTaskId") or ""
    requesting_agent = metadata.get("requestAgent") or metadata.get("requestingAgent") or ""
    entry = {
        "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "agentId": AGENT_ID,
        "operation": operation,
        "taskId": task_id,
        "orchestratorTaskId": orchestrator_task_id,
        "requestingAgent": requesting_agent,
        "target": target,
        "input": input_summary,
        "result": result,
        "durationMs": duration_ms,
    }
    print(f"[{AGENT_ID}] [operation-audit] {json.dumps(entry, ensure_ascii=False)}")
    write_operation_audit(workspace_path, AGENT_ID, entry)


def _handle_llm_dispatch(text: str, message: dict, system_prompt: str) -> tuple[str, list]:
    """Use LLM to understand intent and call the appropriate provider method."""
    try:
        llm_response = _run_agentic(
            prompts.DISPATCH_TEMPLATE.format(
                provider_name=_provider.provider_name,
                user_text=text,
                metadata=json.dumps(message.get("metadata") or {}, ensure_ascii=False, indent=2),
            ),
            AGENT_ID,
            system_prompt=(_build_manifest_prompt(__file__, prompts.DISPATCH_SYSTEM) + "\n\n" + (system_prompt or "")).strip(),
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
    metadata = message.get("metadata") or {}
    workspace_path = metadata.get("sharedWorkspacePath") or ""
    capability = metadata.get("requestedCapability", "")
    configure_control_tools(
        task_context={
            "taskId": task_id,
            "agentId": AGENT_ID,
            "workspacePath": workspace_path,
            "permissions": metadata.get("permissions"),
        },
        complete_fn=lambda result, artifacts: _update_task(task_id, state="TASK_STATE_COMPLETED", message=result),
        fail_fn=lambda error: _update_task(task_id, state="TASK_STATE_FAILED", message=error),
        input_required_fn=lambda question, ctx: _update_task(task_id, state="TASK_STATE_INPUT_REQUIRED", message=question),
    )
    try:
        _update_task(task_id, state="TASK_STATE_WORKING",
                     message="SCM agent is processing the task.")
        if workspace_path:
            record_workspace_stage(
                workspace_path,
                "scm",
                f"Started {capability or 'scm request'}",
                task_id=task_id,
                extra={"runtimeConfig": _runtime_config_summary()},
            )
        # Special handling for async clone
        if capability == "scm.git.clone":
            _dispatch_clone(task_id, message)
            return

        status_text, artifacts = process_message(message)
        _update_task(task_id, state="TASK_STATE_COMPLETED",
                     message=status_text, artifacts=artifacts)
        if workspace_path:
            record_workspace_stage(
                workspace_path,
                "scm",
                f"Completed {capability or 'scm request'}",
                task_id=task_id,
                extra={"statusText": status_text, "runtimeConfig": _runtime_config_summary()},
            )
        _notify_completion(message, task_id, "TASK_STATE_COMPLETED", status_text, artifacts)
    except Exception as error:
        print(f"[{AGENT_ID}] Task {task_id} failed: {error}")
        failure_text = f"SCM agent failed: {error}"
        artifacts = []
        if isinstance(error, PermissionDeniedError):
            artifacts = [build_permission_denied_artifact(error.details, agent_id=AGENT_ID)]
        _update_task(task_id, state="TASK_STATE_FAILED", message=failure_text, artifacts=artifacts)
        if workspace_path:
            record_workspace_stage(
                workspace_path,
                "scm",
                f"Failed {capability or 'scm request'}",
                task_id=task_id,
                extra={"error": str(error), "runtimeConfig": _runtime_config_summary()},
            )
        _notify_completion(message, task_id, "TASK_STATE_FAILED", failure_text, artifacts)


def _dispatch_clone(task_id: str, message: dict):
    """Kick off async git clone."""
    text = extract_text(message)
    metadata = message.get("metadata") or {}
    owner, repo = _parse_owner_repo(text)
    branch = re.search(r"branch[:\s]+([^\s,]+)", text, re.IGNORECASE)
    branch_name = branch.group(1) if branch else "main"
    target = metadata.get("sharedWorkspacePath") or tempfile.mkdtemp(prefix="scm-workspace-")
    callback_url = metadata.get("orchestratorCallbackUrl", "")
    # Depth control: default shallow (depth=1); caller may request full_history or custom depth
    clone_payload = metadata.get("clonePayload") or {}
    full_history = bool(clone_payload.get("fullHistory", False))
    clone_depth = int(clone_payload.get("depth", 1))
    if not owner or not repo:
        _update_task(task_id, state="TASK_STATE_FAILED",
                     message="Could not parse owner/repo for clone.")
        if metadata.get("sharedWorkspacePath"):
            record_workspace_stage(
                metadata.get("sharedWorkspacePath"),
                "scm",
                "Failed scm.git.clone",
                task_id=task_id,
                extra={
                    "error": "Could not parse owner/repo for clone.",
                    "runtimeConfig": _runtime_config_summary(),
                },
            )
        return
    _require_scm_permission(
        action="repo.clone",
        target=f"{owner}/{repo}",
        scope=branch_name,
        message=message,
    )
    # Run the actual clone in a background thread
    t = threading.Thread(
        target=_clone_async_worker,
        args=(task_id, owner, repo, branch_name, target, callback_url),
        kwargs={"depth": clone_depth, "full_history": full_history},
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
            if not _enforce_http_scm_permission(self, action="repo.inspect", target=f"{owner}/{repo}"):
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
            if not _enforce_http_scm_permission(self, action="branch.list", target=f"{effective_owner}/{repo}"):
                return
            branches, status = _provider.list_branches(effective_owner, repo)
            self._send_json(200 if status == "ok" else 500, {"branches": branches, "status": status})
            return

        if path == "/scm/pull-requests":
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            state = qs.get("state", ["open"])[0]
            if not _enforce_http_scm_permission(self, action="pr.list", target=f"{owner}/{repo}"):
                return
            prs, status = _provider.list_prs(owner, repo, state)
            self._send_json(200 if status == "ok" else 500, {"pullRequests": prs, "status": status})
            return

        if re.match(r"^/scm/pull-requests/\d+/comments$", path):
            pr_id = int(path.split("/")[3])
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            if not _enforce_http_scm_permission(self, action="pr.comment.list", target=f"{owner}/{repo}#{pr_id}"):
                return
            comments, status = _provider.list_pr_comments(owner, repo, pr_id)
            self._send_json(200 if status == "ok" else 500, {"comments": comments, "status": status})
            return

        if re.match(r"^/scm/pull-requests/\d+$", path):
            pr_id = int(path.rsplit("/", 1)[-1])
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            if not _enforce_http_scm_permission(self, action="pr.get", target=f"{owner}/{repo}#{pr_id}"):
                return
            pr, status = _provider.get_pr(owner, repo, pr_id)
            self._send_json(200 if status == "ok" else 404, {"pr": pr, "status": status})
            return

        if path == "/scm/repo/tree":
            clone_path = qs.get("path", [""])[0]
            depth = int(qs.get("depth", ["4"])[0])
            if not clone_path or not os.path.isdir(clone_path):
                self._send_json(400, {"error": "invalid clone path"})
                return
            if not _enforce_http_scm_permission(self, action="repo.tree", target=clone_path):
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
            if not _enforce_http_scm_permission(self, action="repo.file", target=f"{clone_path}:{file_path}"):
                return
            content, status = _repo_file(clone_path, file_path)
            if status != "ok":
                self._send_json(404, {"error": status})
                return
            self._send_json(200, {"content": content, "file": file_path})
            return

        # Remote read endpoints (no local clone required)
        if path == "/scm/remote/file":
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            file_path = qs.get("path", [""])[0]
            ref = qs.get("ref", [""])[0]
            if not owner or not repo or not file_path:
                self._send_json(400, {"error": "missing owner/repo/path"})
                return
            if not _enforce_http_scm_permission(
                self, action="repo.read_file", target=f"{owner}/{repo}:{file_path}"
            ):
                return
            content, status = _provider.read_remote_file(owner, repo, file_path, ref)
            if status != "ok":
                self._send_json(404, {"error": status})
                return
            self._send_json(200, {"content": content, "path": file_path, "ref": ref})
            return

        if path == "/scm/remote/dir":
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            dir_path = qs.get("path", [""])[0]
            ref = qs.get("ref", [""])[0]
            if not owner or not repo:
                self._send_json(400, {"error": "missing owner/repo"})
                return
            if not _enforce_http_scm_permission(
                self, action="repo.list_dir", target=f"{owner}/{repo}:{dir_path or '/'}"
            ):
                return
            entries, status = _provider.list_remote_dir(owner, repo, dir_path, ref)
            self._send_json(200 if status == "ok" else 500, {"entries": entries, "status": status})
            return

        if path == "/scm/remote/search":
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            query = qs.get("q", qs.get("query", [""]))[0]
            limit = int(qs.get("limit", ["20"])[0])
            if not owner or not repo or not query:
                self._send_json(400, {"error": "missing owner/repo/q"})
                return
            if not _enforce_http_scm_permission(
                self, action="code.search", target=f"{owner}/{repo}"
            ):
                return
            results, status = _provider.search_code(owner, repo, query, limit)
            self._send_json(200 if status in ("ok", "not_supported") else 500,
                            {"results": results, "status": status})
            return

        if path == "/scm/refs/compare":
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            base = qs.get("base", ["main"])[0]
            head = qs.get("head", [""])[0]
            stat_only = qs.get("statOnly", ["false"])[0].lower() == "true"
            if not owner or not repo or not head:
                self._send_json(400, {"error": "missing owner/repo/head"})
                return
            if not _enforce_http_scm_permission(
                self, action="ref.compare", target=f"{owner}/{repo}:{base}...{head}"
            ):
                return
            result, status = _provider.compare_refs(owner, repo, base, head, stat_only=stat_only)
            self._send_json(200 if status == "ok" else 500, {"comparison": result, "status": status})
            return

        if path == "/scm/branch/default":
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            if not owner or not repo:
                self._send_json(400, {"error": "missing owner/repo"})
                return
            if not _enforce_http_scm_permission(
                self, action="branch.default", target=f"{owner}/{repo}"
            ):
                return
            result, status = _provider.get_default_branch(owner, repo)
            self._send_json(200 if status == "ok" else 500, {"branchInfo": result, "status": status})
            return

        if path == "/scm/branch/rules":
            owner = qs.get("owner", qs.get("project", [""]))[0]
            repo = qs.get("repo", [""])[0]
            if not owner or not repo:
                self._send_json(400, {"error": "missing owner/repo"})
                return
            if not _enforce_http_scm_permission(
                self, action="branch.rules", target=f"{owner}/{repo}"
            ):
                return
            result, status = _provider.get_branch_rules(owner, repo)
            self._send_json(200 if status == "ok" else 500, {"branchRules": result, "status": status})
            return

        # Audit log query endpoint
        if path == "/audit":
            task_id = qs.get("taskId", [""])[0]
            agent_id_qs = qs.get("agentId", [AGENT_ID])[0]
            operation = qs.get("operation", [""])[0]
            since = qs.get("since", [""])[0]
            workspace = qs.get("workspace", [""])[0]
            if not workspace:
                # Try to find a recent workspace from task store
                with TASKS_LOCK:
                    matching = [
                        t for t in TASKS.values()
                        if not task_id or t.get("id") == task_id
                    ]
                # Return in-memory audit from print logs only (no workspace path available)
                self._send_json(200, {
                    "entries": [],
                    "note": "Provide ?workspace=<path> to query persistent audit log",
                })
                return
            entries = read_operation_audit(
                workspace, agent_id_qs,
                task_id=task_id, operation=operation, since=since,
            )
            self._send_json(200, {"entries": entries, "count": len(entries)})
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
            if not _enforce_http_scm_permission(
                self,
                action="branch.create",
                target=f"{owner}/{repo}:{branch}",
                scope=branch,
                payload_permissions=body.get("permissions"),
            ):
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
            if not _enforce_http_scm_permission(
                self,
                action="pr.create",
                target=f"{owner}/{repo}:{from_branch}->{to_branch}",
                payload_permissions=body.get("permissions"),
            ):
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
            if not _enforce_http_scm_permission(
                self,
                action="pr.comment",
                target=f"{owner}/{repo}#{pr_id}",
                scope="self",
                payload_permissions=body.get("permissions"),
            ):
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
            clone_depth = int(body.get("depth", 1))
            full_history = bool(body.get("fullHistory", False))
            if not owner or not repo or not target_path:
                self._send_json(400, {"error": "missing owner/repo/targetPath"})
                return
            if not _enforce_http_scm_permission(
                self,
                action="repo.clone",
                target=f"{owner}/{repo}",
                scope=branch,
                payload_permissions=body.get("permissions"),
            ):
                return
            task_id = _create_task("TASK_STATE_WORKING", f"Cloning {owner}/{repo} …")
            t = threading.Thread(
                target=_clone_async_worker,
                args=(task_id, owner, repo, branch, target_path, callback_url),
                kwargs={"depth": clone_depth, "full_history": full_history},
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
            if not _enforce_http_scm_permission(
                self,
                action="branch.push",
                target=f"{owner}/{repo}:{branch}",
                scope=branch,
                payload_permissions=body.get("permissions"),
            ):
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
