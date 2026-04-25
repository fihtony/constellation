"""Long-running Bitbucket agent — repo inspect, branch create, and PR operations."""

from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from common.devlog import debug_log, preview_data
from common.env_utils import load_dotenv
from common.instance_reporter import InstanceReporter
from common.llm_client import generate_text
from common.message_utils import build_text_artifact, extract_text

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8020"))
AGENT_ID = os.environ.get("AGENT_ID", "bitbucket-agent")
ADVERTISED_URL = os.environ.get("ADVERTISED_BASE_URL", f"http://bitbucket:{PORT}")
BITBUCKET_BASE_URL = os.environ.get("BITBUCKET_BASE_URL", "https://bitbucket.example.com")
BITBUCKET_API_BASE_URL = os.environ.get("BITBUCKET_API_BASE_URL", "")
BITBUCKET_TOKEN = os.environ.get("BITBUCKET_TOKEN", "")
BITBUCKET_USERNAME = os.environ.get("BITBUCKET_USERNAME", "")
BITBUCKET_AUTH_MODE = os.environ.get("BITBUCKET_AUTH_MODE", "auto").strip().lower()
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:8080")
CORP_CA_BUNDLE = (
    os.environ.get("CORP_CA_BUNDLE", "") or os.environ.get("SSL_CERT_FILE", "")
)
# Derive the Bitbucket Server REST API root from base URL
# e.g. https://bitbucket.example.com/projects/MYPROJECT
#   -> https://bitbucket.example.com/rest/api/1.0
_BB_HOST = BITBUCKET_BASE_URL.split("/projects/")[0].rstrip("/") if "/projects/" in BITBUCKET_BASE_URL else BITBUCKET_BASE_URL.rstrip("/")
BITBUCKET_REST_API = os.environ.get(
    "BITBUCKET_REST_API",
    f"{_BB_HOST}/rest/api/1.0",
)

_AGENT_CARD_PATH = os.path.join(os.path.dirname(__file__), "agent-card.json")
_SKILL_GUIDE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    ".github",
    "skills",
    "bitbucket-server-workflow",
    "SKILL.md",
)


def _read_text_file(path):
    if not path or not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _strip_frontmatter(text):
    stripped = (text or "").strip()
    if not stripped.startswith("---\n"):
        return stripped
    parts = stripped.split("\n---\n", 1)
    if len(parts) == 2:
        return parts[1].strip()
    return stripped


def _load_skill_guide(limit=2200):
    text = _strip_frontmatter(_read_text_file(_SKILL_GUIDE_PATH))
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


def _write_workspace_file(workspace_path, relative_name, content):
    if not workspace_path:
        return
    os.makedirs(workspace_path, exist_ok=True)
    target_path = os.path.join(workspace_path, relative_name)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _load_agent_card():
    with open(_AGENT_CARD_PATH, encoding="utf-8") as fh:
        card = json.load(fh)
    text = json.dumps(card).replace("__ADVERTISED_URL__", ADVERTISED_URL)
    return json.loads(text)


def _ssl_ctx():
    ctx = ssl.create_default_context()
    if CORP_CA_BUNDLE and os.path.isfile(CORP_CA_BUNDLE):
        ctx.load_verify_locations(CORP_CA_BUNDLE)
    return ctx


def _bb_auth_header():
    token = (BITBUCKET_TOKEN or "").strip()
    if not token:
        return None

    if token.lower().startswith(("basic ", "bearer ")):
        return token

    use_basic = BITBUCKET_AUTH_MODE == "basic" or (
        BITBUCKET_AUTH_MODE == "auto" and bool(BITBUCKET_USERNAME.strip())
    )
    if use_basic:
        user = BITBUCKET_USERNAME.strip()
        if not user:
            return None
        basic_token = base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
        return f"Basic {basic_token}"

    return f"Bearer {token}"


TASK_SEQ = 0
TASKS = {}
TASKS_LOCK = threading.Lock()
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
PR_URL_RE = re.compile(
    r"^(https?://[^/]+)/projects/([^/]+)/repos/([^/]+)/pull-requests/(\d+)(?:/.*)?$"
)
REPO_URL_RE = re.compile(
    r"(https?://[^\s]+/projects/(?P<project>[^/]+)/repos/(?P<repo>[^/]+)/browse[^\s]*)",
    re.IGNORECASE,
)

_TOKEN_EQUIVALENTS = {
    "sec": {"sec", "secure", "security"},
    "secure": {"sec", "secure", "security"},
    "security": {"sec", "secure", "security"},
    "svc": {"svc", "service", "services"},
    "service": {"svc", "service", "services"},
    "services": {"svc", "service", "services"},
    "util": {"util", "utility", "utilities"},
    "utility": {"util", "utility", "utilities"},
    "utilities": {"util", "utility", "utilities"},
}

# Optional default project for read-only repo search; write paths must always pass project explicitly.
_BB_PROJECT = os.environ.get("BITBUCKET_DEFAULT_PROJECT", "").strip()

def next_task_id():
    global TASK_SEQ
    TASK_SEQ += 1
    return f"bitbucket-task-{TASK_SEQ:04d}"


def _task_message(text):
    return {
        "role": "ROLE_AGENT",
        "parts": [{"text": text}],
    }


def _create_task_record(initial_state, initial_message):
    task_id = next_task_id()
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


def _update_task_record(task_id, state=None, message=None, artifacts=None, extra=None):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return None
        if state is not None:
            task["state"] = state
        if message is not None:
            task["message"] = message
        if artifacts is not None:
            task["artifacts"] = artifacts
        if extra is not None:
            task["extra"] = {**task.get("extra", {}), **extra}
        task["updatedAt"] = time.time()
        return dict(task)


def _task_payload(task_id):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return None
        return {
            "id": task["id"],
            "contextId": task["id"],
            "agentId": task["agentId"],
            "status": {
                "state": task["state"],
                "message": _task_message(task["message"]),
            },
            "artifacts": list(task["artifacts"]),
            "extra": dict(task.get("extra", {})),
            "createdAt": task["createdAt"],
            "updatedAt": task["updatedAt"],
        }


def _post_json_url(url, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8")
        return response.status, json.loads(raw) if raw.strip() else {}


def _notify_orchestrator_completion(message, downstream_task_id, state, status_text, artifacts):
    metadata = message.get("metadata", {})
    callback_url = (metadata.get("orchestratorCallbackUrl") or "").strip()
    if not callback_url:
        orchestrator_task_id = (metadata.get("orchestratorTaskId") or "").strip()
        if not orchestrator_task_id:
            return
        callback_url = f"{ORCHESTRATOR_URL.rstrip('/')}/tasks/{orchestrator_task_id}/callbacks"
    try:
        _post_json_url(
            callback_url,
            {
                "taskId": downstream_task_id,
                "downstreamTaskId": downstream_task_id,
                "agentId": AGENT_ID,
                "state": state,
                "statusMessage": status_text,
                "artifacts": artifacts,
            },
        )
    except Exception as error:
        debug_log(
            AGENT_ID,
            "bitbucket.workflow.callback_failed",
            taskId=downstream_task_id,
            callbackUrl=callback_url,
            error=str(error),
        )


def _run_task_async(task_id, message):
    try:
        _update_task_record(
            task_id,
            state="TASK_STATE_WORKING",
            message="Bitbucket agent is processing the task.",
        )
        status_text, artifacts = process_message(message)
        _update_task_record(
            task_id,
            state="TASK_STATE_COMPLETED",
            message=status_text,
            artifacts=artifacts,
        )
        _notify_orchestrator_completion(
            message,
            task_id,
            "TASK_STATE_COMPLETED",
            status_text,
            artifacts,
        )
    except Exception as error:
        debug_log(AGENT_ID, "bitbucket.workflow.failed", taskId=task_id, error=str(error))
        failure_text = f"Bitbucket agent failed: {error}"
        _update_task_record(
            task_id,
            state="TASK_STATE_FAILED",
            message=failure_text,
            artifacts=[],
        )
        _notify_orchestrator_completion(
            message,
            task_id,
            "TASK_STATE_FAILED",
            failure_text,
            [],
        )


# ---------------------------------------------------------------------------
# Repo listing & URL resolution
# ---------------------------------------------------------------------------

def _expand_tokens(tokens):
    expanded = set()
    for token in tokens:
        token = token.strip().lower()
        if not token:
            continue
        expanded.add(token)
        expanded.update(_TOKEN_EQUIVALENTS.get(token, {token}))
    return expanded


def _list_all_repos(project, page_size=100):
    """Return all repos in a Bitbucket Server project via REST API."""
    if not project:
        return [], "missing_project"
    repos = []
    start = 0
    while True:
        status, body = _bb_request(
            "GET", f"projects/{project}/repos?limit={page_size}&start={start}"
        )
        if status != 200:
            debug_log(AGENT_ID, "bitbucket.repos.list_error",
                      project=project, start=start, status=status, body=preview_data(body))
            return [], f"error_{status}"

        repos.extend(body.get("values", []))
        if body.get("isLastPage", True):
            return repos, "ok"
        start = body.get("nextPageStart")
        if start is None:
            return repos, "ok"


def _repo_urls(project, repo):
    """Build browse URL and git clone URL for a repo dict or slug string."""
    if isinstance(repo, str):
        slug = repo
        clone_href = ""
        for link in []:
            pass
    else:
        slug = repo.get("slug", "")
        # Bitbucket Server provides clone links in repo["links"]["clone"]
        clone_href = ""
        for link in repo.get("links", {}).get("clone", []):
            if link.get("name") == "http":
                clone_href = link.get("href", "")
                break
    browse_url = f"{_BB_HOST}/projects/{project}/repos/{slug}/browse"
    git_url = clone_href or f"{_BB_HOST}/scm/{project.lower()}/{slug}.git"
    return browse_url, git_url


def _score_repo(repo, query_tokens):
    """Score a repo against query tokens. Higher = better match."""
    slug = repo.get("slug", "").lower()
    name = repo.get("name", "").lower()
    # tokenise slug and name on non-alphanumeric boundaries
    repo_tokens = set(re.split(r"[^a-z0-9]+", slug + " " + name))
    repo_tokens.discard("")
    expanded_query = _expand_tokens(query_tokens)
    expanded_repo = _expand_tokens(repo_tokens)
    overlap = len(expanded_query & expanded_repo)

    compact_query = "-".join(sorted(query_tokens))
    bonus = 0
    if compact_query and compact_query in slug:
        bonus += 8
    if all(token in expanded_repo for token in expanded_query):
        bonus += 4
    if slug.startswith("archive-") and "archive" not in query_tokens:
        bonus -= 3
    return overlap + bonus


def resolve_repo_url(query, project=_BB_PROJECT):
    """
    Given a user query string (or full URL), return
    {"browseUrl": ..., "gitUrl": ..., "slug": ..., "matchedName": ..., "score": ...}

    If `query` looks like a full URL, return it directly without API lookup.
    Otherwise, list all repos in `project` and fuzzy-match against `query`.
    """
    # If user provided a full URL, use it as-is
    parsed = urlparse(query or "")
    if parsed.scheme in ("http", "https") and parsed.netloc:
        git_url = ""
        # Try to derive a git URL from a browse URL pattern
        # e.g. .../projects/CSM/repos/SLUG/browse -> .../scm/csm/SLUG.git
        m = re.search(r"/projects/([^/]+)/repos/([^/]+)", parsed.path)
        if m:
            proj, slug = m.group(1), m.group(2)
            git_url = f"{parsed.scheme}://{parsed.netloc}/scm/{proj.lower()}/{slug}.git"
        return {
            "browseUrl": query.split("?")[0],
            "gitUrl": git_url,
            "slug": m.group(2) if m else "",
            "matchedName": "",
            "score": 100,
            "source": "user_provided",
        }

    if not project:
        return {
            "error": "missing_project",
            "browseUrl": "",
            "gitUrl": "",
            "slug": "",
            "matchedName": "",
            "score": 0,
            "source": "missing_project",
        }

    # Normalise query to tokens
    query_lower = (query or "").lower()
    query_tokens = set(re.split(r"[^a-z0-9]+", query_lower))
    query_tokens.discard("")

    repos, list_status = _list_all_repos(project)
    if not repos:
        return {
            "error": list_status,
            "browseUrl": "",
            "gitUrl": "",
            "slug": "",
            "matchedName": "",
            "score": 0,
            "source": "repo_lookup_failed",
        }

    best = max(repos, key=lambda r: _score_repo(r, query_tokens))
    browse_url, git_url = _repo_urls(project, best)
    return {
        "browseUrl": browse_url,
        "gitUrl": git_url,
        "slug": best.get("slug", ""),
        "matchedName": best.get("name", best.get("slug", "")),
        "score": _score_repo(best, query_tokens),
        "source": "fuzzy_match",
    }


def search_repos(query, project=_BB_PROJECT, limit=10):
    parsed = urlparse(query or "")
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return [resolve_repo_url(query, project)], "ok"

    if not project:
        return [], "missing_project"

    query_lower = (query or "").lower()
    query_tokens = set(re.split(r"[^a-z0-9]+", query_lower))
    query_tokens.discard("")

    repos, result = _list_all_repos(project)
    if not repos:
        return [], result

    ranked = []
    for repo in repos:
        score = _score_repo(repo, query_tokens)
        browse_url, git_url = _repo_urls(project, repo)
        ranked.append(
            {
                "slug": repo.get("slug", ""),
                "matchedName": repo.get("name", repo.get("slug", "")),
                "browseUrl": browse_url,
                "gitUrl": git_url,
                "score": score,
                "source": "fuzzy_match",
            }
        )

    ranked.sort(key=lambda item: (-item["score"], item["slug"]))
    return ranked[: max(1, int(limit or 10))], "ok"


def _bb_request(method, path, payload=None):
    """Generic Bitbucket Server REST API call. Returns (http_status, body_dict)."""
    url = f"{BITBUCKET_REST_API.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    auth_header = _bb_auth_header()
    if auth_header:
        headers["Authorization"] = auth_header
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8")
            body = json.loads(raw) if raw.strip() else {}
            return resp.status, body
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"error": raw[:500]}
        return exc.code, body
    except URLError as exc:
        return 0, {"error": str(exc.reason)}


def _extract_repo_url(text):
    match = REPO_URL_RE.search(text or "")
    return match.group(1).split("?", 1)[0] if match else ""


def _require_value(value, field_name):
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"missing {field_name}")
    return normalized


def _get_default_branch(project, repo_slug):
    """Return the default branch name for a repo."""
    status, body = _bb_request(
        "GET", f"projects/{project}/repos/{quote(repo_slug)}/branches/default"
    )
    if status == 200:
        return body.get("displayId", "develop")
    return "develop"


def _list_branches(project, repo_slug):
    status, body = _bb_request(
        "GET", f"projects/{project}/repos/{quote(repo_slug)}/branches?limit=50"
    )
    if status == 200:
        return body.get("values", []), "ok"
    return [], f"error_{status}"


def _create_branch(project, repo_slug, branch_name, start_point="HEAD"):
    payload = {"name": branch_name, "startPoint": start_point}
    status, body = _bb_request(
        "POST",
        f"projects/{project}/repos/{quote(repo_slug)}/branches",
        payload,
    )
    if status in (200, 201):
        debug_log(AGENT_ID, "bitbucket.branch.created",
                  project=project, repo=repo_slug, branch=branch_name)
        return body, "created"
    debug_log(AGENT_ID, "bitbucket.branch.create_error",
              project=project, repo=repo_slug, branch=branch_name,
              status=status, body=preview_data(body))
    return body, f"create_failed_{status}"


def _extract_jira_issue_keys(*texts):
    linked_keys = []
    seen = set()
    for text in texts:
        for key in JIRA_KEY_RE.findall(text or ""):
            if key in seen:
                continue
            seen.add(key)
            linked_keys.append(key)
    return linked_keys


def _branch_display_name(ref_payload):
    if not isinstance(ref_payload, dict):
        return ""
    display_id = ref_payload.get("displayId", "")
    if display_id:
        return display_id
    ref_id = ref_payload.get("id", "")
    if ref_id.startswith("refs/heads/"):
        return ref_id[len("refs/heads/"):]
    return ref_id


def _pr_self_url(payload):
    if not isinstance(payload, dict):
        return ""
    links = payload.get("links", {})
    if not isinstance(links, dict):
        return ""
    self_links = links.get("self", [])
    if not isinstance(self_links, list) or not self_links:
        return ""
    first_link = self_links[0]
    return first_link.get("href", "") if isinstance(first_link, dict) else ""


def _create_pr(project, repo_slug, from_branch, to_branch, title, description=""):
    payload = {
        "title": title,
        "description": description,
        "state": "OPEN",
        "open": True,
        "closed": False,
        "fromRef": {
            "id": f"refs/heads/{from_branch}",
            "repository": {
                "slug": repo_slug,
                "project": {"key": project},
            },
        },
        "toRef": {
            "id": f"refs/heads/{to_branch}",
            "repository": {
                "slug": repo_slug,
                "project": {"key": project},
            },
        },
        "reviewers": [],
    }
    status, body = _bb_request(
        "POST",
        f"projects/{project}/repos/{quote(repo_slug)}/pull-requests",
        payload,
    )
    if status in (200, 201):
        pr_id = body.get("id")
        pr_url = _pr_self_url(body)
        debug_log(AGENT_ID, "bitbucket.pr.created",
                  project=project, repo=repo_slug, prId=pr_id, prUrl=pr_url)
        return body, "created"

    # 409 = PR already open for this branch; find and return existing PR
    if status == 409:
        list_status, list_body = _bb_request(
            "GET",
            f"projects/{project}/repos/{quote(repo_slug)}/pull-requests"
            f"?at=refs/heads/{quote(from_branch)}&direction=OUTGOING&state=OPEN&limit=5",
        )
        if list_status == 200 and isinstance(list_body, dict):
            values = list_body.get("values") or []
            for pr in values:
                if isinstance(pr, dict):
                    pr_id = pr.get("id")
                    pr_url = _pr_self_url(pr)
                    debug_log(AGENT_ID, "bitbucket.pr.existing",
                              project=project, repo=repo_slug, prId=pr_id, prUrl=pr_url)
                    return pr, "existing"
        debug_log(AGENT_ID, "bitbucket.pr.conflict_unresolved",
                  project=project, repo=repo_slug, status=status, body=preview_data(body))
        return body, "create_failed_409"

    debug_log(AGENT_ID, "bitbucket.pr.create_error",
              project=project, repo=repo_slug,
              status=status, body=preview_data(body))
    return body, f"create_failed_{status}"


def _pr_summary(payload):
    if not isinstance(payload, dict):
        return {}
    from_ref = payload.get("fromRef") or {}
    to_ref = payload.get("toRef") or {}
    title = payload.get("title", "")
    description = payload.get("description", "")
    from_branch = _branch_display_name(from_ref)
    author = payload.get("author") or {}
    author_user = author.get("user") if isinstance(author, dict) else {}
    return {
        "id": payload.get("id"),
        "version": payload.get("version"),
        "title": title,
        "description": description,
        "state": payload.get("state"),
        "open": payload.get("open"),
        "closed": payload.get("closed"),
        "fromBranch": from_branch,
        "toBranch": _branch_display_name(to_ref),
        "author": author_user.get("displayName", "") if isinstance(author_user, dict) else "",
        "prUrl": _pr_self_url(payload),
        "linkedJiraIssues": _extract_jira_issue_keys(title, description, from_branch),
    }


def _get_pr(project, repo_slug, pr_id):
    status, body = _bb_request(
        "GET",
        f"projects/{project}/repos/{quote(repo_slug)}/pull-requests/{int(pr_id)}",
    )
    if status == 200:
        return body, "ok"
    return body, f"error_{status}"


def _list_prs(project, repo_slug, state="OPEN", limit=25):
    state_value = (state or "OPEN").upper()
    bounded_limit = max(1, min(int(limit or 25), 100))
    status, body = _bb_request(
        "GET",
        f"projects/{project}/repos/{quote(repo_slug)}/pull-requests?state={quote(state_value)}&limit={bounded_limit}",
    )
    if status == 200:
        return body.get("values", []), "ok"
    return [], f"error_{status}"


def _merge_pr(project, repo_slug, pr_id, version=None):
    pr_version = version
    if pr_version is None:
        pr_body, pr_result = _get_pr(project, repo_slug, pr_id)
        if pr_result != "ok":
            return pr_body, f"merge_blocked_{pr_result}"
        pr_version = pr_body.get("version")
    if pr_version is None:
        return {"error": "missing_pr_version"}, "merge_blocked_missing_version"
    status, body = _bb_request(
        "POST",
        f"projects/{project}/repos/{quote(repo_slug)}/pull-requests/{int(pr_id)}/merge?version={int(pr_version)}",
    )
    if status in (200, 202):
        return body, "merged"
    return body, f"merge_failed_{status}"


def _git_clone_url(project, repo_slug):
    return f"{_BB_HOST}/scm/{project.lower()}/{repo_slug}.git"


def _git_config_args():
    args = []
    auth_header = _bb_auth_header()
    if auth_header:
        args.extend(["-c", f"http.extraHeader=Authorization: {auth_header}"])
    if CORP_CA_BUNDLE and os.path.isfile(CORP_CA_BUNDLE):
        args.extend(["-c", f"http.sslCAInfo={CORP_CA_BUNDLE}"])
    return args


def _run_git(args, cwd=None, timeout=180):
    command = ["git", *_git_config_args(), *args]
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        return False, {"command": command, "returncode": completed.returncode, "output": output}
    return True, {"command": command, "output": output}


def _repo_branch_browse_url(project, repo_slug, branch_name):
    return (
        f"{_BB_HOST}/projects/{project}/repos/{repo_slug}/browse"
        f"?at=refs/heads/{quote(branch_name)}"
    )


def _ensure_relative_repo_path(path_text):
    raw = (path_text or "").strip().lstrip("/")
    if not raw:
        raise ValueError("missing file path")
    candidate = Path(raw)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError(f"unsafe repo path: {path_text}")
    if raw.startswith(".git/"):
        raise ValueError("modifying .git is not allowed")
    return raw


def _git_author_email():
    return (
        os.environ.get("BITBUCKET_GIT_AUTHOR_EMAIL")
        or os.environ.get("GIT_AUTHOR_EMAIL")
        or os.environ.get("JIRA_EMAIL")
        or "bitbucket-agent@local"
    )


def _git_author_name():
    return (
        os.environ.get("BITBUCKET_GIT_AUTHOR_NAME")
        or os.environ.get("GIT_AUTHOR_NAME")
        or "Bitbucket Agent"
    )


def _push_files(project, repo_slug, branch_name, base_branch, files, commit_message, files_to_delete=None):
    workspace = tempfile.mkdtemp(prefix=f"bitbucket-agent-{repo_slug}-")
    repo_dir = os.path.join(workspace, repo_slug)
    clone_url = _git_clone_url(project, repo_slug)
    written_files = []
    deleted_files = []
    try:
        ok, detail = _run_git(["clone", "--depth", "1", "--branch", base_branch, clone_url, repo_dir])
        if not ok:
            return detail, "clone_failed"

        ok, detail = _run_git(["checkout", "-b", branch_name], cwd=repo_dir)
        if not ok:
            return detail, "checkout_failed"

        _run_git(["config", "user.name", _git_author_name()], cwd=repo_dir)
        _run_git(["config", "user.email", _git_author_email()], cwd=repo_dir)

        repo_root = Path(repo_dir).resolve()
        for file_spec in files or []:
            repo_path = _ensure_relative_repo_path(file_spec.get("path", ""))
            dest = (repo_root / repo_path).resolve()
            if repo_root not in dest.parents and dest != repo_root:
                raise ValueError(f"unsafe resolved path: {repo_path}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(file_spec.get("content", ""), encoding="utf-8")
            written_files.append(repo_path)

        # Delete files requested for removal
        for del_path in files_to_delete or []:
            repo_path = _ensure_relative_repo_path(del_path)
            dest = (repo_root / repo_path).resolve()
            if repo_root not in dest.parents and dest != repo_root:
                raise ValueError(f"unsafe resolved path for delete: {repo_path}")
            if dest.is_file():
                ok, detail = _run_git(["rm", "--", repo_path], cwd=repo_dir)
                if ok:
                    deleted_files.append(repo_path)
                else:
                    debug_log(AGENT_ID, "bitbucket.git.push.rm_failed",
                              path=repo_path, detail=detail)

        if not written_files and not deleted_files:
            return {"error": "no files specified"}, "no_files"

        ok, detail = _run_git(["add", "--", *written_files], cwd=repo_dir)
        if not ok:
            return detail, "add_failed"

        ok, detail = _run_git(["status", "--short"], cwd=repo_dir)
        if not ok:
            return detail, "status_failed"
        if not detail.get("output", "").strip():
            return {"files": written_files, "deletedFiles": deleted_files}, "no_changes"

        ok, detail = _run_git(["commit", "-m", commit_message], cwd=repo_dir)
        if not ok:
            return detail, "commit_failed"

        # Force-push: agent branches (agent/feature/...) are agent-owned and may
        # already exist from a prior run; --force is safe here.
        ok, detail = _run_git(["push", "--force", "-u", "origin", branch_name], cwd=repo_dir)
        if not ok:
            return detail, "push_failed"

        ok, detail = _run_git(["rev-parse", "HEAD"], cwd=repo_dir)
        commit_id = detail.get("output", "").strip() if ok else ""
        payload = {
            "project": project,
            "repo": repo_slug,
            "branch": branch_name,
            "baseBranch": base_branch,
            "cloneUrl": clone_url,
            "browseUrl": _repo_branch_browse_url(project, repo_slug, branch_name),
            "commitId": commit_id,
            "files": written_files,
            "deletedFiles": deleted_files,
        }
        debug_log(
            AGENT_ID,
            "bitbucket.git.push.success",
            project=project,
            repo=repo_slug,
            branch=branch_name,
            commitId=commit_id,
            files=written_files,
        )
        return payload, "pushed"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        debug_log(
            AGENT_ID,
            "bitbucket.git.push.error",
            project=project,
            repo=repo_slug,
            branch=branch_name,
            error=str(exc),
        )
        return {"error": str(exc), "files": written_files}, "push_failed"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Async git clone to shared workspace
# ---------------------------------------------------------------------------

CLONE_TIMEOUT_SECONDS = int(os.environ.get("CLONE_TIMEOUT_SECONDS", "600"))
_REPO_TREE_MAX_FILES = int(os.environ.get("REPO_TREE_MAX_FILES", "500"))
_REPO_FILE_MAX_BYTES = int(os.environ.get("REPO_FILE_MAX_BYTES", str(512 * 1024)))  # 512 KB


def _clone_repo_to_workspace(project, repo_slug, branch, target_path):
    """Clone a repo to target_path (persistent, NOT a tempdir).

    Returns (clone_dir, result_str) where clone_dir is the full path to the
    cloned repo on success, or (None, error_str) on failure.
    """
    clone_url = _git_clone_url(project, repo_slug)
    clone_dir = os.path.join(target_path, repo_slug)
    os.makedirs(target_path, exist_ok=True)

    if os.path.isdir(os.path.join(clone_dir, ".git")):
        # Already cloned — just fetch latest
        ok, detail = _run_git(["fetch", "--depth", "1", "origin", branch], cwd=clone_dir,
                              timeout=CLONE_TIMEOUT_SECONDS)
        if not ok:
            return None, f"fetch_failed: {detail.get('output', '')[:200]}"
        ok, detail = _run_git(["checkout", branch], cwd=clone_dir, timeout=30)
        if not ok:
            return None, f"checkout_failed: {detail.get('output', '')[:200]}"
        return clone_dir, "fetched"

    ok, detail = _run_git(
        ["clone", "--depth", "1", "--branch", branch, clone_url, clone_dir],
        timeout=CLONE_TIMEOUT_SECONDS,
    )
    if not ok:
        return None, f"clone_failed: {detail.get('output', '')[:200]}"
    # Expand the fetch refspec so `git fetch` can resolve any branch later
    _run_git(
        ["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
        cwd=clone_dir,
    )
    return clone_dir, "cloned"


def _get_repo_tree(clone_dir, max_depth=4):
    """Return a text representation of the directory tree under clone_dir."""
    clone_root = Path(clone_dir).resolve()
    if not clone_root.is_dir():
        return f"(directory not found: {clone_dir})"
    lines = []
    file_count = 0
    for dirpath, dirnames, filenames in os.walk(str(clone_root)):
        # Skip .git and hidden dirs in-place
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".")
        )
        rel = Path(dirpath).relative_to(clone_root)
        depth = len(rel.parts)
        if depth >= max_depth:
            dirnames.clear()
            continue
        indent = "  " * depth
        if depth > 0:
            lines.append(f"{indent}{rel.parts[-1]}/")
        for fname in sorted(filenames):
            if file_count >= _REPO_TREE_MAX_FILES:
                lines.append(f"{indent}  ... (truncated)")
                return "\n".join(lines)
            lines.append(f"{indent}  {fname}")
            file_count += 1
    return "\n".join(lines) if lines else "(empty repository)"


def _get_repo_file(clone_dir, file_path):
    """Read a single file from the cloned repo.

    Returns (content_str, result) where result is 'ok', 'not_found', 'too_large', or 'unsafe_path'.
    """
    try:
        safe_path = _ensure_relative_repo_path(file_path)
    except ValueError as exc:
        return "", f"unsafe_path: {exc}"
    clone_root = Path(clone_dir).resolve()
    target = (clone_root / safe_path).resolve()
    if clone_root not in target.parents and target != clone_root:
        return "", "unsafe_path: traversal detected"
    if not target.is_file():
        return "", "not_found"
    size = target.stat().st_size
    if size > _REPO_FILE_MAX_BYTES:
        return "", f"too_large: {size} bytes"
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "", f"read_error: {exc}"
    return content, "ok"


def _fire_clone_callback(callback_url, task_id, state, clone_dir, error_msg):
    """Fire clone completion callback. Best-effort — errors are logged, not raised."""
    if not callback_url:
        return
    payload = {
        "taskId": task_id,
        "agentId": AGENT_ID,
        "state": state,
        "clonePath": clone_dir or "",
        "error": error_msg or "",
    }
    try:
        _post_json_url(callback_url, payload)
        debug_log(AGENT_ID, "bitbucket.clone.callback_sent",
                  taskId=task_id, callbackUrl=callback_url, state=state)
    except Exception as exc:
        debug_log(AGENT_ID, "bitbucket.clone.callback_failed",
                  taskId=task_id, callbackUrl=callback_url, error=str(exc))


def _run_clone_async(task_id, project, repo_slug, branch, target_path, callback_url):
    """Background worker: clone repo, update task record, fire callback."""
    try:
        _update_task_record(task_id, state="TASK_STATE_WORKING",
                            message=f"Cloning {project}/{repo_slug} branch={branch} …")
        clone_dir, result = _clone_repo_to_workspace(project, repo_slug, branch, target_path)
        if clone_dir:
            _update_task_record(
                task_id,
                state="TASK_STATE_COMPLETED",
                message=f"Cloned {project}/{repo_slug} to {clone_dir} ({result})",
                extra={"clonePath": clone_dir, "result": result},
            )
            _fire_clone_callback(callback_url, task_id,
                                 "TASK_STATE_COMPLETED", clone_dir, "")
        else:
            _update_task_record(
                task_id,
                state="TASK_STATE_FAILED",
                message=f"Clone failed: {result}",
                extra={"clonePath": "", "result": result},
            )
            _fire_clone_callback(callback_url, task_id,
                                 "TASK_STATE_FAILED", "", result)
    except Exception as exc:
        error_msg = str(exc)
        debug_log(AGENT_ID, "bitbucket.clone.async_error",
                  taskId=task_id, error=error_msg)
        _update_task_record(task_id, state="TASK_STATE_FAILED",
                            message=f"Clone error: {error_msg}",
                            extra={"clonePath": "", "result": "exception"})
        _fire_clone_callback(callback_url, task_id, "TASK_STATE_FAILED", "", error_msg)


def _get_pr_diff_structured(project, repo_slug, pr_id):
    return _bb_request(
        "GET",
        f"projects/{project}/repos/{quote(repo_slug)}/pull-requests/{pr_id}/diff?limit=1000",
    )


def _parse_pr_url(pr_url):
    match = PR_URL_RE.match((pr_url or "").strip())
    if not match:
        return {"error": "invalid_pr_url", "url": pr_url}, "invalid_pr_url"
    base_url, project, repo_slug, pr_id = match.groups()
    return {
        "baseUrl": base_url.rstrip("/"),
        "project": project,
        "repo": repo_slug,
        "prId": int(pr_id),
        "browseUrl": f"{base_url.rstrip('/')}/projects/{project}/repos/{repo_slug}/pull-requests/{pr_id}",
        "restApiBase": f"{base_url.rstrip('/')}/rest/api/1.0",
        "serverType": "bitbucket",
    }, "ok"


def _list_pr_comments(project, repo_slug, pr_id, page_size=100):
    comments = []
    start = 0
    while True:
        status, body = _bb_request(
            "GET",
            f"projects/{project}/repos/{quote(repo_slug)}/pull-requests/{int(pr_id)}/activities"
            f"?limit={int(page_size)}&start={int(start)}",
        )
        if status != 200:
            return body, f"error_{status}"

        values = body.get("values", []) if isinstance(body, dict) else []
        for activity in values:
            if not isinstance(activity, dict):
                continue
            if activity.get("action") != "COMMENTED":
                continue
            comment = activity.get("comment") or {}
            author = comment.get("author") or {}
            author_user = author.get("user") if isinstance(author, dict) else {}
            anchor = comment.get("anchor") if isinstance(comment.get("anchor"), dict) else None
            comments.append(
                {
                    "id": comment.get("id"),
                    "text": comment.get("text", ""),
                    "anchor": anchor,
                    "createdDate": comment.get("createdDate"),
                    "author": (
                        author_user.get("displayName")
                        if isinstance(author_user, dict)
                        else ""
                    ) or (
                        author_user.get("name") if isinstance(author_user, dict) else ""
                    ),
                }
            )

        if body.get("isLastPage", True):
            return comments, "ok"
        next_page_start = body.get("nextPageStart")
        if next_page_start is None:
            return comments, "ok"
        start = next_page_start


def _normalize_comment_text(text):
    return " ".join((text or "").split()).strip().lower()


def _comment_anchor_path(anchor):
    if not isinstance(anchor, dict):
        return ""
    raw_path = anchor.get("path") or anchor.get("srcPath") or anchor.get("file") or ""
    return str(raw_path).lstrip("/")


def _comment_anchor_line(anchor):
    if not isinstance(anchor, dict):
        return None
    for key in ("line", "lineNumber", "toLine", "fromLine"):
        value = anchor.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _comment_anchor_matches(anchor, file_path="", line=None):
    target_path = ""
    if file_path:
        try:
            target_path = _ensure_relative_repo_path(file_path)
        except ValueError:
            return False

    target_line = None
    if line not in (None, "", 0):
        try:
            target_line = int(str(line))
        except (TypeError, ValueError):
            return False

    anchor_path = _comment_anchor_path(anchor)
    anchor_line = _comment_anchor_line(anchor)
    if not target_path and target_line is None:
        return not anchor_path and anchor_line is None
    if target_path and anchor_path != target_path:
        return False
    if target_line is not None and anchor_line != target_line:
        return False
    return True


def _find_duplicate_comments(existing_comments, text, *, file_path="", line=None):
    normalized_target = _normalize_comment_text(text)
    if not normalized_target:
        return []

    matches = []
    for comment in existing_comments:
        comment_text = _normalize_comment_text(comment.get("text", ""))
        if not comment_text:
            continue
        exact_match = comment_text == normalized_target
        substring_match = len(normalized_target) >= 24 and (
            normalized_target in comment_text or comment_text in normalized_target
        )
        if not (exact_match or substring_match):
            continue
        if not _comment_anchor_matches(comment.get("anchor"), file_path=file_path, line=line):
            continue
        matches.append(comment)
    return matches


def _find_line_in_diff(file_path, target_line, diff_data):
    normalized_path = _ensure_relative_repo_path(file_path)
    diffs = diff_data.get("diffs", []) if isinstance(diff_data, dict) else []
    for diff in diffs:
        destination = diff.get("destination") or {}
        path_value = destination.get("toString", "")
        if path_value != normalized_path:
            continue

        properties = diff.get("properties") or {}
        for hunk in diff.get("hunks", []):
            segments = hunk.get("segments", [])
            for segment in segments:
                segment_type = segment.get("type", "CONTEXT")
                for line_info in segment.get("lines", []):
                    if line_info.get("destination") != target_line:
                        continue
                    if segment_type == "ADDED":
                        line_type = "ADDED"
                    elif segment_type == "REMOVED":
                        line_type = "REMOVED"
                    else:
                        line_type = "CONTEXT"
                    return {
                        "path": normalized_path,
                        "line": target_line,
                        "lineType": line_type,
                        "fromHash": properties.get("fromHash"),
                        "toHash": properties.get("toHash"),
                    }
    return None


def _post_pr_comment(project, repo_slug, pr_id, text, *, file_path="", line=None):
    payload = {"text": text}
    inline_requested = bool(file_path and line)
    if inline_requested:
        try:
            target_line = int(str(line))
        except (TypeError, ValueError):
            target_line = 0
        if target_line > 0:
            diff_status, diff_data = _get_pr_diff_structured(project, repo_slug, pr_id)
            if diff_status == 200:
                line_info = _find_line_in_diff(file_path, target_line, diff_data)
                if line_info:
                    anchor = {
                        "line": line_info["line"],
                        "lineType": line_info["lineType"],
                        "fileType": "TO",
                        "path": line_info["path"],
                        "diffType": "EFFECTIVE",
                    }
                    if line_info.get("fromHash"):
                        anchor["fromHash"] = line_info["fromHash"]
                    if line_info.get("toHash"):
                        anchor["toHash"] = line_info["toHash"]
                    payload["anchor"] = anchor

    path = f"projects/{project}/repos/{quote(repo_slug)}/pull-requests/{pr_id}/comments"
    status, body = _bb_request("POST", path, payload)
    if status in (200, 201):
        return body, "created_inline" if "anchor" in payload else "created"

    if "anchor" in payload:
        status, body = _bb_request("POST", path, {"text": text})
        if status in (200, 201):
            return body, "created_fallback"
    return body, f"create_failed_{status}"


def process_message(message):
    user_text = extract_text(message)
    debug_log(AGENT_ID, "bitbucket.message.received", userText=user_text)

    metadata = message.get("metadata", {})
    workspace_path = (metadata.get("sharedWorkspacePath") or "").strip()
    repo_url = (
        metadata.get("repoUrl")
        or metadata.get("bitbucketRepoUrl")
        or metadata.get("browseUrl")
        or _extract_repo_url(user_text)
    )

    for artifact in metadata.get("upstreamArtifacts", []):
        artifact_metadata = artifact.get("metadata") or {}
        repo_url = (
            repo_url
            or artifact_metadata.get("repoUrl")
            or artifact_metadata.get("bitbucketRepoUrl")
            or artifact_metadata.get("browseUrl")
            or _extract_repo_url(artifact.get("text", ""))
        )

    # Resolve repo URL from explicit user or upstream context only.
    resolved = resolve_repo_url(repo_url)
    browse_url = resolved["browseUrl"]
    git_url = resolved["gitUrl"]
    matched_name = resolved.get("matchedName", "")
    source = resolved.get("source", "")

    skill_guide = _load_skill_guide()

    prompt = f"""
You are the Bitbucket Agent in a Constellation multi-agent software delivery system.
Use the resolved Bitbucket CSM project repository details to help downstream execution agents.

Operational skill guide:
{skill_guide or 'No local skill guide loaded.'}

User request:
{user_text}

Resolved repository:
  Name:       {matched_name}
  Browse URL: {browse_url}
  Git clone:  {git_url}
  Match source: {source}

Return a concise engineering handoff with these sections:
1. Repo choice and why it matches the request
2. Browse URL and Git clone URL for the team
3. Suggested next implementation step
""".strip()

    summary = generate_text(prompt, "Bitbucket Agent")
    if not repo_url:
        summary = (
            "No explicit Bitbucket repo browse URL was found in the request or upstream artifacts. "
            "Provide the full repo URL so the Bitbucket agent can resolve the target safely.\n\n"
            f"LLM summary:\n{summary}"
        )
    elif resolved.get("error"):
        summary = (
            f"Bitbucket repo resolution failed safely with '{resolved['error']}'. "
            "Provide an explicit repo URL or project-qualified search input.\n\n"
            f"LLM summary:\n{summary}"
        )
    status_text = f"Bitbucket analysis completed. Repo: {matched_name or browse_url}"
    artifacts = [
        build_text_artifact(
            "bitbucket-summary",
            summary,
            artifact_type="application/vnd.multi-agent.summary",
            metadata={
                "agentId": AGENT_ID,
                "browseUrl": browse_url,
                "gitUrl": git_url,
                "matchedName": matched_name,
                "source": source,
            },
        ),
        build_text_artifact(
            "bitbucket-repo-url",
            browse_url,
            artifact_type="text/uri-list",
            metadata={"agentId": AGENT_ID, "gitUrl": git_url},
        ),
    ]
    if workspace_path:
        _write_workspace_file(workspace_path, "bitbucket/bitbucket-summary.md", summary)
        _write_workspace_file(
            workspace_path,
            "bitbucket/repo-resolution.json",
            json.dumps(resolved, ensure_ascii=False, indent=2),
        )
    debug_log(
        AGENT_ID, "bitbucket.message.completed",
        browseUrl=browse_url, gitUrl=git_url, source=source,
    )
    return status_text, artifacts


class BitbucketHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok", "agent_id": AGENT_ID})
            return
        task_match = re.fullmatch(r"/tasks/([^/]+)", path)
        if task_match:
            task = _task_payload(task_match.group(1))
            if task:
                self._send_json(200, {"task": task})
            else:
                self._send_json(404, {"error": "task_not_found"})
            return
        if path == "/.well-known/agent-card.json":
            self._send_json(200, _load_agent_card())
            return

        # GET /bitbucket/repo/tree?path=<clone_dir>&depth=4
        if path == "/bitbucket/repo/tree":
            qs = parse_qs(urlparse(self.path).query)
            clone_path = (qs.get("path") or [""])[0].strip()
            depth = int((qs.get("depth") or ["4"])[0])
            if not clone_path:
                self._send_json(400, {"error": "missing path parameter"})
                return
            tree_text = _get_repo_tree(clone_path, max_depth=min(depth, 6))
            self._send_json(200, {"clonePath": clone_path, "tree": tree_text,
                                  "skill": "bitbucket.repo.tree", "executionMode": "sync"})
            return

        # GET /bitbucket/repo/file?path=<clone_dir>&file=<relative_file_path>
        if path == "/bitbucket/repo/file":
            qs = parse_qs(urlparse(self.path).query)
            clone_path = (qs.get("path") or [""])[0].strip()
            file_path = (qs.get("file") or [""])[0].strip()
            if not clone_path or not file_path:
                self._send_json(400, {"error": "missing path or file parameter"})
                return
            content, result = _get_repo_file(clone_path, file_path)
            if result != "ok":
                self._send_json(404 if result == "not_found" else 400,
                                {"clonePath": clone_path, "file": file_path, "result": result})
                return
            self._send_json(200, {"clonePath": clone_path, "file": file_path,
                                  "content": content, "result": "ok",
                                  "skill": "bitbucket.repo.file", "executionMode": "sync"})
            return

        # GET /bitbucket/branches?project=X&repo=Y
        if path == "/bitbucket/branches":
            qs = parse_qs(urlparse(self.path).query)
            project = (qs.get("project") or [""])[0].strip()
            repo = (qs.get("repo") or [""])[0].strip()
            if not project or not repo:
                self._send_json(400, {"error": "missing project or repo"})
                return
            branches, result = _list_branches(project, repo)
            self._send_json(200 if result == "ok" else 502,
                            {"project": project, "repo": repo,
                             "result": result, "branches": branches})
            return

        # GET /bitbucket/repo-url?q=<text or full URL>[&project=CSM]
        if path == "/bitbucket/repo-url":
            qs = parse_qs(urlparse(self.path).query)
            query = (qs.get("q") or qs.get("query") or [""])[0]
            project = (qs.get("project") or [""])[0].strip()
            if not query:
                self._send_json(400, {"error": "missing q parameter"})
                return
            resolved = resolve_repo_url(query, project)
            self._send_json(200 if not resolved.get("error") else 422, resolved)
            return

        # GET /bitbucket/search/repos?q=<text>&project=MYPROJECT&limit=10
        if path == "/bitbucket/search/repos":
            qs = parse_qs(urlparse(self.path).query)
            query = (qs.get("q") or qs.get("query") or [""])[0]
            project = (qs.get("project") or [""])[0].strip()
            limit = int((qs.get("limit") or ["10"])[0])
            if not query:
                self._send_json(400, {"error": "missing q parameter"})
                return
            if not project:
                self._send_json(400, {"error": "missing project"})
                return
            repos, result = search_repos(query, project, limit)
            self._send_json(
                200 if result == "ok" else 502,
                {"project": project, "query": query, "result": result, "repos": repos},
            )
            return

        # GET /bitbucket/repos?project=CSM — list all repos
        if path == "/bitbucket/repos":
            qs = parse_qs(urlparse(self.path).query)
            project = (qs.get("project") or [""])[0].strip()
            if not project:
                self._send_json(400, {"error": "missing project"})
                return
            repos, result = _list_all_repos(project)
            slim = [
                {
                    "slug": r.get("slug"),
                    "name": r.get("name"),
                    "browseUrl": f"{_BB_HOST}/projects/{project}/repos/{r.get('slug')}/browse",
                    "gitUrl": next(
                        (lnk["href"] for lnk in r.get("links", {}).get("clone", [])
                         if lnk.get("name") == "http"),
                        f"{_BB_HOST}/scm/{project.lower()}/{r.get('slug')}.git",
                    ),
                }
                for r in repos
            ]
            self._send_json(200 if result == "ok" else 502,
                            {"project": project, "result": result, "repos": slim})
            return

        # GET /bitbucket/pull-requests?project=MYPROJECT&repo=sample-app&state=OPEN&limit=25
        if path == "/bitbucket/pull-requests":
            qs = parse_qs(urlparse(self.path).query)
            project = (qs.get("project") or [""])[0].strip()
            repo = (qs.get("repo") or [""])[0].strip()
            state = (qs.get("state") or ["OPEN"])[0]
            limit = int((qs.get("limit") or ["25"])[0])
            if not project or not repo:
                self._send_json(400, {"error": "missing project or repo"})
                return
            pull_requests, result = _list_prs(project, repo, state=state, limit=limit)
            self._send_json(
                200 if result == "ok" else 502,
                {
                    "project": project,
                    "repo": repo,
                    "state": state.upper(),
                    "result": result,
                    "pullRequests": [_pr_summary(pr) for pr in pull_requests if isinstance(pr, dict)],
                },
            )
            return

        # GET /bitbucket/pull-requests/parse?url=https://.../pull-requests/123
        if path == "/bitbucket/pull-requests/parse":
            qs = parse_qs(urlparse(self.path).query)
            pr_url = (qs.get("url") or [""])[0]
            if not pr_url:
                self._send_json(400, {"error": "missing url parameter"})
                return
            parsed, result = _parse_pr_url(pr_url)
            self._send_json(200 if result == "ok" else 422, {"result": result, "pullRequest": parsed})
            return

        # GET /bitbucket/pull-requests/{id}?project=MYPROJECT&repo=sample-app
        m = re.fullmatch(r"/bitbucket/pull-requests/(\d+)", path)
        if m:
            qs = parse_qs(urlparse(self.path).query)
            project = (qs.get("project") or [""])[0].strip()
            repo = (qs.get("repo") or [""])[0].strip()
            if not project or not repo:
                self._send_json(400, {"error": "missing project or repo"})
                return
            pr_id = int(m.group(1))
            result_body, result = _get_pr(project, repo, pr_id)
            self._send_json(
                200 if result == "ok" else 502,
                {
                    "project": project,
                    "repo": repo,
                    "prId": pr_id,
                    "result": result,
                    "pullRequest": _pr_summary(result_body) if result == "ok" else result_body,
                    "detail": result_body,
                },
            )
            return

        # GET /bitbucket/pull-requests/{id}/comments?project=MYPROJECT&repo=sample-app
        m = re.fullmatch(r"/bitbucket/pull-requests/(\d+)/comments", path)
        if m:
            qs = parse_qs(urlparse(self.path).query)
            project = (qs.get("project") or [""])[0].strip()
            repo = (qs.get("repo") or [""])[0].strip()
            if not project or not repo:
                self._send_json(400, {"error": "missing project or repo"})
                return
            pr_id = int(m.group(1))
            comments, result = _list_pr_comments(project, repo, pr_id)
            self._send_json(
                200 if result == "ok" else 502,
                {
                    "project": project,
                    "repo": repo,
                    "prId": pr_id,
                    "result": result,
                    "comments": comments if result == "ok" else [],
                    "detail": comments if result != "ok" else None,
                },
            )
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/message:send":
            body = self._read_body()
            message = body.get("message", {})
            if not message:
                self._send_json(400, {"error": "missing message"})
                return
            configuration = body.get("configuration") or {}
            if configuration.get("returnImmediately"):
                task_id = _create_task_record(
                    "TASK_STATE_ACCEPTED",
                    "Bitbucket agent accepted the task and will continue asynchronously.",
                )
                worker = threading.Thread(
                    target=_run_task_async,
                    args=(task_id, message),
                    daemon=True,
                )
                worker.start()
                self._send_json(200, {"task": _task_payload(task_id)})
                return
            status_text, artifacts = process_message(message)
            self._send_json(200, {
                "task": {
                    "id": next_task_id(), "agentId": AGENT_ID,
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"text": status_text}]},
                    },
                    "artifacts": artifacts,
                }
            })
            return

        # POST /bitbucket/branches  body: {"project":"MYPROJECT","repo":"sample-app","branch":"...","startPoint":"develop"}
        if path == "/bitbucket/branches":
            body = self._read_body()
            try:
                project = _require_value(body.get("project"), "project")
                repo = _require_value(body.get("repo"), "repo")
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            branch = body.get("branch", "")
            start = body.get("startPoint", "develop")
            if not branch:
                self._send_json(400, {"error": "missing branch name"})
                return
            result_body, result = _create_branch(project, repo, branch, start)
            self._send_json(201 if result == "created" else 502,
                            {"project": project, "repo": repo,
                             "branch": branch, "result": result, "detail": result_body})
            return

        # POST /bitbucket/pull-requests/{id}/merge body: {"project":"MYPROJECT","repo":"sample-app","version":3}
        m = re.fullmatch(r"/bitbucket/pull-requests/(\d+)/merge", path)
        if m:
            body = self._read_body()
            try:
                project = _require_value(body.get("project"), "project")
                repo = _require_value(body.get("repo"), "repo")
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            version = body.get("version")
            pr_id = int(m.group(1))
            result_body, result = _merge_pr(project, repo, pr_id, version=version)
            self._send_json(
                200 if result == "merged" else 502,
                {
                    "project": project,
                    "repo": repo,
                    "prId": pr_id,
                    "result": result,
                    "pullRequest": _pr_summary(result_body) if result == "merged" else result_body,
                    "detail": result_body,
                },
            )
            return

        # POST /bitbucket/pull-requests  body: {"project":"MYPROJECT","repo":"sample-app","fromBranch":"...","toBranch":"develop","title":"..."}
        if path == "/bitbucket/pull-requests":
            body = self._read_body()
            try:
                project = _require_value(body.get("project"), "project")
                repo = _require_value(body.get("repo"), "repo")
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            from_branch = body.get("fromBranch", "")
            to_branch = body.get("toBranch", _get_default_branch(project, repo))
            title = body.get("title", f"Agent PR from {from_branch}")
            description = body.get("description", "")
            if not from_branch:
                self._send_json(400, {"error": "missing fromBranch"})
                return
            result_body, result = _create_pr(
                project, repo, from_branch, to_branch, title, description
            )
            pr_url = _pr_self_url(result_body)
            self._send_json(201 if result == "created" else 502,
                            {"project": project, "repo": repo,
                             "fromBranch": from_branch, "toBranch": to_branch,
                             "result": result, "prUrl": pr_url,
                             "pullRequest": _pr_summary(result_body) if result == "created" else result_body,
                             "detail": result_body})
            return

        # POST /bitbucket/git/clone (ASYNC) body: {"project":"MYPROJECT","repo":"sample-app","branch":"develop","targetPath":"/workspace/task-001","callbackUrl":"http://android:8030/clone-callbacks/android-task-0001"}
        if path == "/bitbucket/git/clone":
            body = self._read_body()
            try:
                project = _require_value(body.get("project"), "project")
                repo = _require_value(body.get("repo"), "repo")
                target_path = _require_value(body.get("targetPath"), "targetPath")
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            branch = body.get("branch") or _get_default_branch(project, repo)
            callback_url = (body.get("callbackUrl") or "").strip()
            task_id = _create_task_record(
                "TASK_STATE_ACCEPTED",
                f"Cloning {project}/{repo} branch={branch} — accepted",
            )
            threading.Thread(
                target=_run_clone_async,
                args=(task_id, project, repo, branch, target_path, callback_url),
                daemon=True,
            ).start()
            self._send_json(202, {
                "taskId": task_id,
                "project": project,
                "repo": repo,
                "branch": branch,
                "targetPath": target_path,
                "state": "TASK_STATE_ACCEPTED",
                "pollUrl": f"GET /tasks/{task_id}",
                "skill": "bitbucket.git.clone",
                "executionMode": "async",
            })
            return

        # POST /bitbucket/git/push body: {"project":"MYPROJECT","repo":"sample-app","branch":"...","baseBranch":"develop","files":[{"path":"...","content":"..."}],"filesToDelete":["app/.gitignore"],"commitMessage":"..."}
        if path == "/bitbucket/git/push":
            body = self._read_body()
            try:
                project = _require_value(body.get("project"), "project")
                repo = _require_value(body.get("repo"), "repo")
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            branch = body.get("branch", "")
            base_branch = body.get("baseBranch") or _get_default_branch(project, repo)
            files = body.get("files") or []
            files_to_delete = body.get("filesToDelete") or []
            commit_message = body.get("commitMessage") or f"Agent update for {branch or repo}"
            if not branch:
                self._send_json(400, {"error": "missing branch"})
                return
            result_body, result = _push_files(project, repo, branch, base_branch, files, commit_message, files_to_delete=files_to_delete)
            self._send_json(
                201 if result == "pushed" else 502,
                {
                    "project": project,
                    "repo": repo,
                    "branch": branch,
                    "baseBranch": base_branch,
                    "result": result,
                    "detail": result_body,
                },
            )
            return

        # POST /bitbucket/pull-requests/comments body: {"project":"MYPROJECT","repo":"sample-app","prId":123,"text":"...","filePath":"...","line":2}
        if path == "/bitbucket/pull-requests/comments/check-duplicates":
            body = self._read_body()
            try:
                project = _require_value(body.get("project"), "project")
                repo = _require_value(body.get("repo"), "repo")
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            pr_id = body.get("prId")
            text = body.get("text", "")
            file_path = body.get("filePath", "")
            line = body.get("line")
            if not pr_id:
                self._send_json(400, {"error": "missing prId"})
                return
            if not text:
                self._send_json(400, {"error": "missing text"})
                return
            comments, result = _list_pr_comments(project, repo, int(pr_id))
            if result != "ok":
                self._send_json(
                    502,
                    {
                        "project": project,
                        "repo": repo,
                        "prId": pr_id,
                        "result": result,
                        "detail": comments,
                    },
                )
                return
            matches = _find_duplicate_comments(comments, text, file_path=file_path, line=line)
            self._send_json(
                200,
                {
                    "project": project,
                    "repo": repo,
                    "prId": pr_id,
                    "result": "ok",
                    "duplicate": bool(matches),
                    "matchCount": len(matches),
                    "matchingComments": matches,
                },
            )
            return

        if path == "/bitbucket/pull-requests/comments":
            body = self._read_body()
            try:
                project = _require_value(body.get("project"), "project")
                repo = _require_value(body.get("repo"), "repo")
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            pr_id = body.get("prId")
            text = body.get("text", "")
            file_path = body.get("filePath", "")
            line = body.get("line")
            if not pr_id:
                self._send_json(400, {"error": "missing prId"})
                return
            if not text:
                self._send_json(400, {"error": "missing text"})
                return
            result_body, result = _post_pr_comment(
                project,
                repo,
                int(pr_id),
                text,
                file_path=file_path,
                line=line,
            )
            comment_id = result_body.get("id") if isinstance(result_body, dict) else None
            self._send_json(
                201 if result.startswith("created") else 502,
                {
                    "project": project,
                    "repo": repo,
                    "prId": pr_id,
                    "result": result,
                    "commentId": comment_id,
                    "detail": result_body,
                },
            )
            return

            self._send_json(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        # Suppress noisy health-check and agent-card polls
        line = args[0] if args else ""
        if any(p in line for p in ("/health", "/.well-known/agent-card.json")):
            return
        print(f"[bitbucket-agent] {line} {args[1] if len(args) > 1 else ''} {args[2] if len(args) > 2 else ''}")


def main():
    print(f"[bitbucket-agent] Bitbucket Agent starting on {HOST}:{PORT}")
    reporter = InstanceReporter(agent_id=AGENT_ID, service_url=ADVERTISED_URL, port=PORT)
    reporter.start()
    server = ThreadingHTTPServer((HOST, PORT), BitbucketHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()