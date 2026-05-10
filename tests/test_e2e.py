#!/usr/bin/env python3
"""Constellation multi-agent end-to-end validation.

Usage:
    python3 tests/test_e2e.py                  # run all tests including the configured workflow
    python3 tests/test_e2e.py -v               # verbose JSON output
    python3 tests/test_e2e.py --smoke-only     # prerequisites + registry only (fast)
    python3 tests/test_e2e.py --workflow-only  # only run the configured ticket workflow test
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.task_permissions import load_permission_grant

TESTS_ENV = PROJECT_ROOT / "tests" / ".env"
COMMON_ENV = PROJECT_ROOT / "common" / ".env"

COMPASS_URL = "http://localhost:8080"
REGISTRY_URL = "http://localhost:9000"
JIRA_URL = "http://localhost:8010"
SCM_URL = "http://localhost:8020"
UI_DESIGN_URL = "http://localhost:8040"

TASK_POLL_TIMEOUT = 120
WORKFLOW_POLL_TIMEOUT = int(os.environ.get("WORKFLOW_POLL_TIMEOUT", "3600"))

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
SMOKE_ONLY = "--smoke-only" in sys.argv
WORKFLOW_ONLY = "--workflow-only" in sys.argv


# ---------------------------------------------------------------------------
# Config from tests/.env
# ---------------------------------------------------------------------------

def _load_env_file(path: Path) -> dict:
    env: dict = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def _parse_scm_target(repo_url: str) -> dict:
    repo_url = str(repo_url or "").strip().rstrip("/")
    if not repo_url:
        return {"provider": "", "owner": "", "repo": "", "base_url": ""}

    bitbucket_match = re.search(r"^(https?://[^/]+)/projects/([^/]+)/repos/([^/?#]+)", repo_url)
    if bitbucket_match:
        repo = bitbucket_match.group(3)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return {
            "provider": "bitbucket",
            "owner": bitbucket_match.group(2),
            "repo": repo,
            "base_url": bitbucket_match.group(1),
        }

    github_match = re.search(r"^https?://github\.com/([^/]+)/([^/?#]+)", repo_url)
    if github_match:
        repo = github_match.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return {
            "provider": "github",
            "owner": github_match.group(1),
            "repo": repo,
            "base_url": "https://github.com",
        }

    return {"provider": "", "owner": "", "repo": "", "base_url": ""}


_ENV = _load_env_file(TESTS_ENV)
_COMMON_ENV = _load_env_file(COMMON_ENV)
_DEVELOPMENT_PERMISSION_HEADERS = {
    "X-Task-Permissions": json.dumps(load_permission_grant("development").to_dict(), ensure_ascii=False)
}

JIRA_TICKET_URL = _ENV.get("TEST_JIRA_TICKET_URL", "").strip()
JIRA_TICKET_KEY = JIRA_TICKET_URL.rstrip("/").split("/")[-1] if JIRA_TICKET_URL else ""
JIRA_BASE_URL   = "/".join(JIRA_TICKET_URL.split("/")[:3]) if JIRA_TICKET_URL else ""
JIRA_API_BASE   = f"{JIRA_BASE_URL}/rest/api/3" if JIRA_BASE_URL else ""
JIRA_TOKEN      = _ENV.get("TEST_JIRA_TOKEN", "").strip()
JIRA_EMAIL      = _ENV.get("TEST_JIRA_EMAIL", "").strip()

GITHUB_REPO_URL = _ENV.get("TEST_GITHUB_REPO_URL", "").strip()
GITHUB_TOKEN    = _ENV.get("TEST_GITHUB_TOKEN", "").strip()
DESIGN_URL      = (
    _ENV.get("TEST_DESIGN_URL", "")
    or _ENV.get("TEST_FIGMA_FILE_URL", "")
    or _ENV.get("TEST_STITCH_PROJECT_URL", "")
).strip()
SCM_REPO_URL    = GITHUB_REPO_URL.strip()
SCM_TOKEN       = GITHUB_TOKEN.strip()
SCM_TARGET      = _parse_scm_target(SCM_REPO_URL)
SCM_PROVIDER    = SCM_TARGET.get("provider", "")
SCM_PROVIDER_LABEL = {"github": "GitHub", "bitbucket": "Bitbucket"}.get(SCM_PROVIDER, "SCM")
SCM_OWNER       = SCM_TARGET.get("owner", "")
SCM_REPO        = SCM_TARGET.get("repo", "")
SCM_BASE_URL    = SCM_TARGET.get("base_url", "")

HOST_ARTIFACT_ROOT = str(PROJECT_ROOT / "artifacts")
LOCAL_TIMEZONE = (
    os.environ.get("LOCAL_TIMEZONE", "").strip()
    or _ENV.get("LOCAL_TIMEZONE", "").strip()
    or _COMMON_ENV.get("LOCAL_TIMEZONE", "").strip()
)

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

passed = 0
failed = 0
errors: list = []


class Colors:
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"


def section(title: str):
    print(f"\n{Colors.BOLD}{'=' * 64}{Colors.RESET}")
    print(f"{Colors.BOLD}  {title}{Colors.RESET}")
    print(f"{Colors.BOLD}{'=' * 64}{Colors.RESET}")


def step(desc: str):
    print(f"\n  {Colors.CYAN}->{Colors.RESET} {desc}")


def ok(msg: str):
    global passed
    passed += 1
    print(f"  {Colors.GREEN}PASS{Colors.RESET} -- {msg}")


def fail(msg: str, detail: str = ""):
    global failed
    failed += 1
    errors.append(msg)
    print(f"  {Colors.RED}FAIL{Colors.RESET} -- {msg}")
    if detail:
        print(f"         {detail}")


def info(msg: str):
    print(f"  {Colors.YELLOW}INFO{Colors.RESET} {msg}")


def warn(msg: str):
    print(f"  {Colors.YELLOW}WARN{Colors.RESET} {msg}")


def show_json(label: str, data):
    if VERBOSE:
        print(f"     {label}:")
        print(textwrap.indent(json.dumps(data, ensure_ascii=False, indent=2), "       "))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_json(url: str, method: str = "GET", payload=None,
              timeout: int = 30, headers: dict | None = None):
    data = None
    h: dict = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        h.update(headers)
    try:
        req = Request(url, data=data, headers=h, method=method)
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw[:300]}
        return e.code, body
    except (URLError, OSError) as e:
        return 0, {"error": str(e)}


def send_message(
    text: str,
    requested_capability: str | None = None,
    timeout: int = 30,
    context_id: str | None = None,
):
    payload = {
        "message": {
            "messageId": f"e2e-{int(time.time() * 1000)}",
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
    }
    if requested_capability:
        payload["requestedCapability"] = requested_capability
    if context_id:
        payload["contextId"] = context_id
    return http_json(f"{COMPASS_URL}/message:send", method="POST",
                     payload=payload, timeout=timeout)


def poll_task(tid: str, timeout: int = TASK_POLL_TIMEOUT,
              print_progress: bool = False) -> dict | None:
    terminal = {
        "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "FAILED", "COMPLETED",
        "NO_CAPABLE_AGENT", "CAPACITY_EXHAUSTED",
        "CAPABILITY_TEMPORARILY_UNAVAILABLE", "POLICY_DENIED",
        "TASK_STATE_INPUT_REQUIRED",
    }
    deadline = time.time() + timeout
    last_state = ""
    last_prog = 0
    last_body = None
    while time.time() < deadline:
        status, body = http_json(f"{COMPASS_URL}/tasks/{tid}", timeout=10)
        if status == 200:
            last_body = body
            task = body.get("task", {})
            state = task.get("status", {}).get("state", "")
            if print_progress:
                steps = task.get("progressSteps", [])
                if len(steps) > last_prog:
                    for s in steps[last_prog:]:
                        print(f"     [{s.get('agentId','?')}] {s.get('step','')}")
                    last_prog = len(steps)
            if state != last_state:
                info(f"Task {tid} state: {state}")
                last_state = state
            if state in terminal:
                return body
        time.sleep(3)
    status, body = http_json(f"{COMPASS_URL}/tasks/{tid}", timeout=10)
    if status == 200:
        return body
    return last_body


def t_state(body: dict | None) -> str:
    return (body or {}).get("task", {}).get("status", {}).get("state", "")


def t_id(body: dict | None) -> str:
    return (body or {}).get("task", {}).get("id", "")


def t_workspace(body: dict | None) -> str:
    workspace = (body or {}).get("task", {}).get("workspacePath", "")
    if workspace:
        return workspace
    for step in reversed((body or {}).get("task", {}).get("progressSteps", [])):
        step_text = step.get("step", "") if isinstance(step, dict) else ""
        match = re.search(r"(/app/artifacts/workspaces/[A-Za-z0-9._-]+)", step_text)
        if match:
            return match.group(1)
    return ""


def t_agent(body: dict | None) -> str:
    return (body or {}).get("task", {}).get("agentId", "")


def container_to_host(container_path: str) -> str:
    if not container_path:
        return ""
    prefix = "/app/artifacts"
    if container_path.startswith(prefix):
        return HOST_ARTIFACT_ROOT + container_path[len(prefix):]
    return container_path


def _read_json_file(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _extract_repo_workspace_path(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("repoWorkspacePath", "clonedRepoPath", "workspacePath", "repoPath"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _task_message_text(body: dict | None) -> str:
    parts = (body or {}).get("task", {}).get("status", {}).get("message", {}).get("parts", [])
    if not isinstance(parts, list):
        return ""
    return "\n".join(
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("text")
    ).strip()


# Non-execution agent directory names (never the dev-agent workspace dir).
_NON_EXEC_AGENT_DIRS = frozenset([
    "compass", "team-lead", "jira", "scm", "ui-design", "registry",
])


def _find_exec_agent_dir(host_ws: str) -> str:
    """Return the relative name of the execution dev-agent subdirectory.

    Scans all immediate subdirs for pr-evidence.json or jira-actions.json,
    excluding known non-execution agent dirs. Falls back to 'web-agent' so
    existing tests gracefully degrade.
    """
    if not host_ws:
        return "web-agent"
    ws = Path(host_ws)
    for sub in sorted(ws.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name in _NON_EXEC_AGENT_DIRS:
            continue
        # Skip the cloned repo dir (it has a .git subdir, not agent artifacts)
        if (sub / ".git").is_dir():
            continue
        if (sub / "pr-evidence.json").is_file() or (sub / "jira-actions.json").is_file():
            return sub.name
    return "web-agent"


def _normalize_text(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _jira_doc_to_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_jira_doc_to_text(item) for item in value) if part).strip()
    if not isinstance(value, dict):
        return ""

    parts: list[str] = []
    node_type = str(value.get("type") or "")
    if node_type == "text" and value.get("text"):
        parts.append(str(value.get("text") or ""))
    if node_type == "inlineCard":
        url = str((value.get("attrs") or {}).get("url") or "").strip()
        if url:
            parts.append(url)
    for item in value.get("content") or []:
        child_text = _jira_doc_to_text(item)
        if child_text:
            parts.append(child_text)
    return "\n".join(parts).strip()


def _ticket_stack_expectations(issue: dict | None) -> dict:
    fields = (issue or {}).get("fields") or {}
    text_blob = "\n".join(
        part
        for part in [
            str(fields.get("summary") or "").strip(),
            _jira_doc_to_text(fields.get("description") or {}),
        ]
        if part
    ).lower()
    expectations: dict[str, str] = {}
    if "react.js" in text_blob or "reactjs" in text_blob or re.search(r"\breact\b", text_blob):
        expectations["frontend_framework"] = "react"
    elif "next.js" in text_blob or "nextjs" in text_blob:
        expectations["frontend_framework"] = "nextjs"
    elif re.search(r"\bvue(?:\.js)?\b", text_blob):
        expectations["frontend_framework"] = "vue"

    if re.search(r"\bpython\s*3\.12\b", text_blob):
        expectations["language"] = "python"
        expectations["python_version"] = "3.12"
    elif re.search(r"\bpython\b", text_blob):
        expectations["language"] = "python"

    if re.search(r"\bflask\b", text_blob):
        expectations["backend_framework"] = "flask"
    elif re.search(r"\bfastapi\b", text_blob):
        expectations["backend_framework"] = "fastapi"
    elif re.search(r"\bexpress(?:\.js)?\b", text_blob):
        expectations["backend_framework"] = "express"
    return expectations


def _assert_expected_stack_constraints(constraints: dict, expectations: dict, label: str) -> None:
    if not expectations:
        warn(f"{label} has no explicit stack constraints in Jira; skipping ticket-specific checks")
        return
    for key, expected in expectations.items():
        actual = str(constraints.get(key) or "").strip()
        if key in {"frontend_framework", "backend_framework", "language"}:
            if _normalize_text(actual) == _normalize_text(expected):
                ok(f"{label} captured Jira-required {key}: {expected}")
            else:
                fail(f"{label} did not capture Jira-required {key}", f"expected={expected!r}, actual={actual!r}")
        elif actual == expected:
            ok(f"{label} captured Jira-required {key}: {expected}")
        else:
            fail(f"{label} did not capture Jira-required {key}", f"expected={expected!r}, actual={actual!r}")


def _assert_matching_stack_constraints(plan_constraints: dict, runtime_constraints: dict) -> None:
    for key in ("language", "python_version", "backend_framework", "frontend_framework"):
        left = str(plan_constraints.get(key) or "").strip()
        right = str(runtime_constraints.get(key) or "").strip()
        if not left and not right:
            continue
        if key in {"language", "backend_framework", "frontend_framework"}:
            if _normalize_text(left) == _normalize_text(right):
                ok(f"Team Lead and Web Agent agree on {key}: {left or right}")
            else:
                fail(f"Team Lead and Web Agent disagree on {key}", f"team-lead={left!r}, web-agent={right!r}")
        elif left == right:
            ok(f"Team Lead and Web Agent agree on {key}: {left}")
        else:
            fail(f"Team Lead and Web Agent disagree on {key}", f"team-lead={left!r}, web-agent={right!r}")


def _assert_runtime_target(runtime_summary: dict, label: str) -> None:
    requested_backend = str(runtime_summary.get("requestedBackend") or "").strip()
    effective_backend = str(runtime_summary.get("effectiveBackend") or "").strip()
    model = str(runtime_summary.get("model") or "").strip()

    _valid_backends = {"copilot-cli", "connect-agent"}
    if requested_backend in _valid_backends:
        ok(f"{label} requested backend is {requested_backend}")
    else:
        fail(f"{label} requested backend is not a valid agent backend", f"actual={requested_backend!r}")

    if effective_backend in _valid_backends:
        ok(f"{label} effective backend is {effective_backend}")
    else:
        fail(f"{label} effective backend is not a valid agent backend", f"actual={effective_backend!r}")

    if model == "gpt-5-mini":
        ok(f"{label} model is gpt-5-mini")
    else:
        fail(f"{label} model is not gpt-5-mini", f"actual={model!r}")


def _latest_completed_jira_event(events: list[dict], action: str) -> dict | None:
    for event in reversed(events):
        if event.get("action") == action and event.get("status") == "completed":
            return event
    return None


def _assert_local_timestamp_value(raw_value: str, label: str) -> bool:
    if not raw_value:
        fail(f"{label} timestamp missing")
        return False
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError as exc:
        fail(f"{label} timestamp is not ISO-8601", str(exc))
        return False

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        fail(f"{label} timestamp missing timezone offset", raw_value)
        return False

    if not LOCAL_TIMEZONE:
        ok(f"{label} timestamp includes timezone offset: {raw_value}")
        return True

    try:
        local_dt = parsed.astimezone(ZoneInfo(LOCAL_TIMEZONE))
    except ZoneInfoNotFoundError:
        warn(f"Configured LOCAL_TIMEZONE {LOCAL_TIMEZONE!r} is invalid; only checking offset presence")
        ok(f"{label} timestamp includes timezone offset: {raw_value}")
        return True

    if parsed.utcoffset() == local_dt.utcoffset():
        ok(f"{label} timestamp uses {LOCAL_TIMEZONE}: {raw_value}")
        return True

    fail(
        f"{label} timestamp not in {LOCAL_TIMEZONE}",
        f"value={raw_value}, expected_offset={local_dt.utcoffset()}",
    )
    return False


def _assert_local_timestamp_field(host_ws: str, rel: str, field: str, label: str) -> bool:
    payload = _read_json_file(os.path.join(host_ws, rel))
    if not isinstance(payload, dict):
        fail(f"{label} JSON unreadable", rel)
        return False
    return _assert_local_timestamp_value(str(payload.get(field) or ""), label)


def _assert_local_timestamp_from_events(host_ws: str, rel: str, label: str) -> bool:
    payload = _read_json_file(os.path.join(host_ws, rel))
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or not events:
        fail(f"{label} has no recorded events", rel)
        return False
    latest = events[-1] if isinstance(events[-1], dict) else {}
    return _assert_local_timestamp_value(str(latest.get("ts") or ""), label)


# ---------------------------------------------------------------------------
# Jira API helpers
# ---------------------------------------------------------------------------

def _jira_h() -> dict:
    if not JIRA_TOKEN or not JIRA_EMAIL:
        return {}
    cred = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


def jira_get_issue(key: str) -> dict | None:
    if JIRA_TOKEN and JIRA_EMAIL:
        s, b = http_json(f"{JIRA_API_BASE}/issue/{key}?fields=status,assignee,comment,summary,description",
                         headers=_jira_h())
        if s == 200:
            return b
    s, b = http_json(f"{JIRA_URL}/jira/tickets/{key}", headers=_DEVELOPMENT_PERMISSION_HEADERS)
    issue = b.get("issue") if isinstance(b, dict) else None
    return issue if s == 200 and isinstance(issue, dict) else None


def jira_get_comments(key: str) -> list:
    issue = jira_get_issue(key) or {}
    comment_block = issue.get("fields", {}).get("comment") or {}
    comments = comment_block.get("comments")
    return comments if isinstance(comments, list) else []


# ---------------------------------------------------------------------------
# SCM API helpers
# ---------------------------------------------------------------------------

def _scm_h() -> dict:
    if not SCM_TOKEN:
        return {}
    if SCM_PROVIDER == "bitbucket":
        return {"Authorization": f"Bearer {SCM_TOKEN}"}
    return {"Authorization": f"token {SCM_TOKEN}"}


def scm_prs(owner: str, repo: str, state: str = "open") -> list:
    if SCM_PROVIDER == "bitbucket":
        bb_state = "OPEN" if state.lower() == "open" else "MERGED" if state.lower() in ("merged", "closed") else "ALL"
        s, b = http_json(
            f"{SCM_BASE_URL}/rest/api/1.0/projects/{owner}/repos/{quote(repo)}/pull-requests?state={bb_state}&limit=30",
            headers=_scm_h(),
        )
        if s == 200 and isinstance(b, dict):
            result = []
            for pr in b.get("values", []):
                pr_id = pr.get("id")
                if pr_id is None:
                    continue
                result.append({
                    "number": pr_id,
                    "title": pr.get("title", ""),
                    "html_url": f"{SCM_BASE_URL}/projects/{owner}/repos/{repo}/pull-requests/{pr_id}",
                    "body": pr.get("description") or "",
                })
            return result
        return []

    s, b = http_json(
        f"https://api.github.com/repos/{owner}/{repo}/pulls?state={state}&per_page=30",
        headers=_scm_h(),
    )
    return b if (s == 200 and isinstance(b, list)) else []


def scm_branches(owner: str, repo: str) -> list:
    if SCM_PROVIDER == "bitbucket":
        s, b = http_json(
            f"{SCM_BASE_URL}/rest/api/1.0/projects/{owner}/repos/{quote(repo)}/branches?limit=100",
            headers=_scm_h(),
        )
        return [x.get("displayId", "") for x in b.get("values", [])] if (s == 200 and isinstance(b, dict)) else []

    s, b = http_json(
        f"https://api.github.com/repos/{owner}/{repo}/branches?per_page=100",
        headers=_scm_h(),
    )
    return [x["name"] for x in b] if (s == 200 and isinstance(b, list)) else []


# ---------------------------------------------------------------------------
# Scenario 0: Prerequisites
# ---------------------------------------------------------------------------

def test_0_prerequisites() -> bool:
    section("Scenario 0: Prerequisites Check")
    all_ok = True
    for label, url in [
        ("Registry",        f"{REGISTRY_URL}/health"),
        ("Compass",         f"{COMPASS_URL}/health"),
        ("Jira Agent",      f"{JIRA_URL}/health"),
        ("SCM Agent",       f"{SCM_URL}/health"),
        ("UI Design Agent", f"{UI_DESIGN_URL}/health"),
    ]:
        step(f"Check {label} health")
        status, body = http_json(url)
        if status == 200:
            ok(f"{label} is healthy")
            show_json(label, body)
        else:
            fail(f"{label} is not reachable", f"status={status}")
            if label in ("Registry", "Compass"):
                all_ok = False

    step("Verify team-lead-agent is registered in Registry (per-task)")
    s, body = http_json(f"{REGISTRY_URL}/agents")
    agent_ids = {a.get("agent_id") for a in (body if isinstance(body, list) else [])}
    if "team-lead-agent" in agent_ids:
        ok("team-lead-agent is registered in Registry")
    else:
        warn("team-lead-agent not in Registry -- will be registered after rebuild")
    return all_ok


# ---------------------------------------------------------------------------
# Scenario 1: Agent Card Discovery
# ---------------------------------------------------------------------------

def test_1_agent_card_discovery():
    section("Scenario 1: Agent Card Discovery")
    for label, url, name in [
        ("Compass",         f"{COMPASS_URL}/.well-known/agent-card.json",    "Compass Agent"),
        ("Jira Agent",      f"{JIRA_URL}/.well-known/agent-card.json",       "Jira Agent"),
        ("SCM Agent",       f"{SCM_URL}/.well-known/agent-card.json",        "SCM Agent"),
        ("UI Design Agent", f"{UI_DESIGN_URL}/.well-known/agent-card.json",  "UI Design Agent"),
    ]:
        step(f"Fetch {label} agent card")
        s, b = http_json(url)
        show_json(label, b)
        if s == 200 and b.get("name") == name:
            ok(f"{label} agent card OK")
        else:
            fail(f"Unexpected {label} card", f"status={s}, name={b.get('name')!r}")


# ---------------------------------------------------------------------------
# Scenario 2: Registry State
# ---------------------------------------------------------------------------

def test_2_registry_state():
    section("Scenario 2: Registry State Verification")
    step("List all agent definitions")
    s, body = http_json(f"{REGISTRY_URL}/agents")
    show_json("Definitions", body)
    if s != 200 or not isinstance(body, list):
        fail("Failed to list registry definitions")
        return
    defs = {d["agent_id"]: d for d in body}

    for agent_id in ("jira-agent", "scm-agent"):
        step(f"Check {agent_id} is registered with a live instance")
        if agent_id not in defs:
            fail(f"{agent_id} missing from registry")
            continue
        s2, instances = http_json(f"{REGISTRY_URL}/agents/{agent_id}/instances")
        if s2 == 200 and isinstance(instances, list) and instances:
            ok(f"{agent_id} has a live instance")
        else:
            fail(f"{agent_id} has no live instance")

    step("Check team-lead-agent registered as per-task")
    if "team-lead-agent" in defs:
        mode = defs["team-lead-agent"].get("execution_mode", "")
        if mode == "per-task":
            ok("team-lead-agent is per-task")
        else:
            fail(f"team-lead-agent wrong execution_mode: {mode!r} (expected 'per-task')")
    else:
        fail("team-lead-agent missing from registry")

    step("Check web-agent registered as per-task")
    if "web-agent" in defs:
        mode = defs["web-agent"].get("execution_mode", "")
        if mode == "per-task":
            ok("web-agent is per-task")
        else:
            fail(f"web-agent wrong execution_mode: {mode!r}")
    elif "android-agent" in defs:
        mode = defs["android-agent"].get("execution_mode", "")
        if mode == "per-task":
            ok("android-agent is per-task (web-agent not registered)")
        else:
            fail(f"android-agent wrong execution_mode: {mode!r}")
    else:
        warn("web-agent not in registry (run build-agents.sh first)")


# ---------------------------------------------------------------------------
# Scenario 3: Jira direct routing
# ---------------------------------------------------------------------------

def test_3_jira_capability():
    section("Scenario 3: Jira Capability Direct Routing")
    step(f"Route {JIRA_TICKET_KEY} fetch to jira-agent")
    s, body = send_message(f"Fetch ticket {JIRA_TICKET_KEY}",
                           requested_capability="jira.ticket.fetch")
    show_json("Initial", body)
    if s != 200:
        fail("Jira capability request failed", f"status={s}")
        return
    tid = t_id(body)
    if not tid:
        fail("No task id in response")
        return
    info(f"Polling {tid}...")
    final = poll_task(tid)
    show_json("Final", final)
    if not final:
        fail("Jira task timed out")
        return
    state = t_state(final)
    agent = t_agent(final)
    if state == "TASK_STATE_COMPLETED" and agent == "jira-agent":
        ok(f"Jira capability routed correctly (agent={agent})")
    else:
        fail("Jira capability routing failed", f"state={state}, agent={agent}")


# ---------------------------------------------------------------------------
# Scenario 4: SCM direct routing
# ---------------------------------------------------------------------------

def test_4_scm_capability():
    section("Scenario 4: SCM Capability Direct Routing")
    step(f"Route SCM repo inspect for {GITHUB_REPO_URL}")
    s, body = send_message(f"Inspect the repository {GITHUB_REPO_URL}",
                           requested_capability="scm.repo.inspect")
    show_json("Initial", body)
    if s != 200:
        fail("SCM capability request failed", f"status={s}")
        return
    tid = t_id(body)
    if not tid:
        fail("No task id in response")
        return
    info(f"Polling {tid}...")
    final = poll_task(tid)
    show_json("Final", final)
    if not final:
        fail("SCM task timed out")
        return
    state = t_state(final)
    agent = t_agent(final)
    if state == "TASK_STATE_COMPLETED" and agent == "scm-agent":
        ok(f"SCM capability routed correctly (agent={agent})")
    else:
        fail("SCM capability routing failed", f"state={state}, agent={agent}")


# ---------------------------------------------------------------------------
# Scenario 5: Missing capability
# ---------------------------------------------------------------------------

def test_5_missing_capability():
    section("Scenario 5: Missing Capability Handling")
    step("Request an unregistered capability")
    s, body = send_message("Please inspect the OpenShift cluster.",
                           requested_capability="openshift.cluster.inspect")
    show_json("Response", body)
    if s != 200:
        fail("Unexpected HTTP error", f"status={s}")
        return
    tid = t_id(body)
    init_state = t_state(body)
    if init_state == "NO_CAPABLE_AGENT":
        ok("Missing capability returns NO_CAPABLE_AGENT immediately")
        return
    final = poll_task(tid, timeout=20) if tid else body
    state = t_state(final) if final else init_state
    if state == "NO_CAPABLE_AGENT":
        ok("Missing capability reported as NO_CAPABLE_AGENT")
    else:
        fail("Missing capability not handled correctly", f"state={state}")


# ---------------------------------------------------------------------------
# Scenario 6: Browser UI
# ---------------------------------------------------------------------------

def test_6_browser_ui():
    section("Scenario 6: Browser UI")
    step("Fetch Compass Web UI")
    try:
        req = Request(f"{COMPASS_URL}/", method="GET")
        with urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8")
            status = resp.status
    except Exception as exc:
        fail("Browser UI unavailable", str(exc))
        return
    if status == 200 and "Compass Agent" in html:
        ok("Compass Web UI served")
    else:
        fail("Browser UI unexpected", f"status={status}")


# ---------------------------------------------------------------------------
# Scenario 7: Malformed request
# ---------------------------------------------------------------------------

def test_7_malformed_request():
    section("Scenario 7: Malformed Request")
    step("Send empty body to Compass")
    s, _ = http_json(f"{COMPASS_URL}/message:send", method="POST", payload={})
    if s == 400:
        ok("Malformed request returns HTTP 400")
    else:
        fail("Malformed request did not return 400", f"status={s}")


# ---------------------------------------------------------------------------
# Scenario: Full E2E Workflow Validation
# ---------------------------------------------------------------------------

def _ws_file_ok(ws_host: str, rel: str, label: str) -> bool:
    full = os.path.join(ws_host, rel)
    if os.path.isfile(full):
        ok(f"{label} saved ({os.path.getsize(full)} bytes): {rel}")
        return True
    fail(f"{label} not found in workspace", f"expected: {full}")
    return False


def _ws_file_ok_or_warn(ws_host: str, rel: str, label: str) -> bool:
    """Like _ws_file_ok but uses warn instead of fail when the file is absent."""
    full = os.path.join(ws_host, rel)
    if os.path.isfile(full):
        ok(f"{label} saved ({os.path.getsize(full)} bytes): {rel}")
        return True
    warn(f"{label} not found in workspace (optional artifact may not have been generated)")
    return False


def _validate_workflow_configuration() -> bool:
    missing: list[str] = []
    if not JIRA_TICKET_URL:
        missing.append("TEST_JIRA_TICKET_URL")
    if missing:
        fail(
            "Workflow test configuration missing",
            "Set the following keys in tests/.env: " + ", ".join(missing),
        )
        return False
    return True


def test_ticket_full_workflow():  # noqa: C901
    if not _validate_workflow_configuration():
        return

    section(f"{JIRA_TICKET_KEY or 'Configured Ticket'} Full E2E Workflow Validation")
    print(f"\n  Ticket:  {JIRA_TICKET_URL}")
    print(f"  Repo:    {GITHUB_REPO_URL}")
    print(f"  Design:  {DESIGN_URL or '(none)'}")
    print(f"  Timeout: {WORKFLOW_POLL_TIMEOUT}s\n")

    # ── Baseline ──────────────────────────────────────────────────────────
    step("Record Jira state BEFORE test")
    j_before = jira_get_issue(JIRA_TICKET_KEY)
    j_status_before = ""
    j_assignee_before = ""
    j_comments_before = 0
    if j_before:
        j_status_before = j_before.get("fields", {}).get("status", {}).get("name", "")
        assignee_before = j_before.get("fields", {}).get("assignee") or {}
        j_assignee_before = assignee_before.get("accountId", "")
        j_comments_before = j_before.get("fields", {}).get("comment", {}).get("total", 0)
        info(
            "Jira before: "
            f"status={j_status_before!r}, assignee={j_assignee_before or assignee_before.get('displayName', '')!r}, "
            f"comments={j_comments_before}"
        )
    else:
        warn("Could not fetch Jira baseline (check TEST_JIRA_TOKEN in tests/.env)")
    expected_constraints = _ticket_stack_expectations(j_before)

    step(f"Record {SCM_PROVIDER_LABEL} PRs/branches BEFORE test")
    prs_before: list = []
    branches_before: list = []
    if SCM_OWNER and SCM_REPO and SCM_TOKEN:
        prs_before = [str(p.get("number")) for p in scm_prs(SCM_OWNER, SCM_REPO)]
        branches_before = scm_branches(SCM_OWNER, SCM_REPO)
        info(f"{SCM_PROVIDER_LABEL} before: {len(prs_before)} open PRs, {len(branches_before)} branches")
    else:
        warn("SCM repo URL or token missing -- PR verification skipped")

    # ── a. Submit task ────────────────────────────────────────────────────
    _request_text = f"implement jira ticket: {JIRA_TICKET_URL}"
    step(f"a. Submit '{_request_text}'")
    s, body = send_message(_request_text, timeout=30)
    show_json("Compass response", body)
    if s != 200:
        fail("Compass rejected the task", f"HTTP {s}")
        return
    tid = t_id(body)
    if not tid:
        fail("No task id in Compass response")
        return
    ok(f"Task created: {tid}")

    step("Polling task -- agent progress below:")
    final = poll_task(tid, timeout=WORKFLOW_POLL_TIMEOUT, print_progress=True)
    if not final:
        fail(f"Task did not complete within {WORKFLOW_POLL_TIMEOUT}s")
    else:
        show_json("Final task", final)

    final_state = t_state(final) if final else "TIMEOUT"
    input_required = final_state == "TASK_STATE_INPUT_REQUIRED"
    if final_state == "TASK_STATE_COMPLETED":
        ok(f"Task completed (state={final_state})")
    elif input_required:
        fail(
            "Task asked for clarification instead of proceeding from Jira/design/repo context",
            _task_message_text(final)[:300],
        )
    elif final_state == "TASK_STATE_FAILED":
        parts = (final or {}).get("task", {}).get("status", {}).get("message", {}).get("parts", [{}])
        fail("Task failed", (parts[0].get("text", "") if parts else "")[:200])
    else:
        fail(f"Unexpected final state: {final_state}")

    # ── a. Workspace exists ───────────────────────────────────────────────
    step("a. Verify shared workspace created under artifacts/workspaces/")
    container_ws = t_workspace(final) if final else ""
    host_ws = container_to_host(container_ws)
    info(f"Container ws: {container_ws or '(not in task)'}")
    info(f"Host ws:      {host_ws or '(unmapped)'}")

    if host_ws and os.path.isdir(host_ws):
        ok(f"Workspace exists: {host_ws}")
    else:
        ws_root = Path(HOST_ARTIFACT_ROOT) / "workspaces"
        candidates = sorted(ws_root.glob(f"{tid}*"), reverse=True) if ws_root.is_dir() else []
        if candidates:
            host_ws = str(candidates[0])
            ok(f"Workspace found by task ID: {host_ws}")
        else:
            fail("Workspace not found under artifacts/workspaces/",
                 f"Searched: {HOST_ARTIFACT_ROOT}/workspaces/{tid}*")

    if not host_ws:
        warn("Skipping workspace content checks")
        _verify_external(j_status_before, j_comments_before, prs_before,
                         branches_before, "", final_state, tid)
        return

    # ── b. Jira ticket content ────────────────────────────────────────────
    step("b. Verify Jira ticket content saved to workspace")
    jira_ws = (
        os.path.isfile(os.path.join(host_ws, "jira/jira-summary.md"))
        or os.path.isfile(os.path.join(host_ws, "team-lead/jira-context.json"))
    )
    if jira_ws:
        for rel in ("jira/jira-summary.md", "team-lead/jira-context.json"):
            p = os.path.join(host_ws, rel)
            if os.path.isfile(p):
                ok(f"Jira content in workspace ({os.path.getsize(p)} bytes): {rel}")
                break
    else:
        fail("Jira content NOT found in workspace",
             "Check: jira service needs ./artifacts volume mount in docker-compose.yml")

    # ── c. Design content ─────────────────────────────────────────────────
    step("c. Verify design context saved to workspace")
    design_ws = (
        os.path.isfile(os.path.join(host_ws, "team-lead/design-context.json"))
        or os.path.isfile(os.path.join(host_ws, "ui-design/stitch-design.json"))
    )
    if design_ws:
        ok("Design context saved to workspace")
    else:
        warn("Design context not in workspace (Stitch may be unavailable or page not needed)")

    # Team Lead plan
    _ws_file_ok(host_ws, "team-lead/stage-summary.json", "Team Lead stage summary")
    _ws_file_ok(host_ws, "team-lead/command-log.txt", "Team Lead command log")
    plan_path = os.path.join(host_ws, "team-lead/plan.json")
    review_notes_path = os.path.join(host_ws, "team-lead/review-notes.json")
    repo_context_path = os.path.join(host_ws, "team-lead/repo-context.json")
    if input_required:
        if os.path.isfile(plan_path):
            ok("Team Lead implementation plan saved before clarification")
        else:
            warn("Team Lead implementation plan not created yet (expected while waiting for clarification)")
        if os.path.isfile(review_notes_path):
            warn("Team Lead review notes exist before dev dispatch")
        else:
            ok("No Team Lead review notes yet (expected before dev dispatch)")
    else:
        _ws_file_ok(host_ws, "team-lead/plan.json", "Team Lead implementation plan")
        _ws_file_ok(host_ws, "team-lead/review-notes.json", "Team Lead review notes")
        _ws_file_ok(host_ws, "team-lead/repo-context.json", "Team Lead repo handoff context")

    step("b3. Verify Team Lead runtime target and skill playbooks")
    team_lead_stage_payload = _read_json_file(os.path.join(host_ws, "team-lead/stage-summary.json"))
    team_lead_runtime_payload = team_lead_stage_payload.get("runtimeConfig") if isinstance(team_lead_stage_payload, dict) else None
    if isinstance(team_lead_runtime_payload, dict):
        runtime_summary = team_lead_runtime_payload.get("runtime") if isinstance(team_lead_runtime_payload.get("runtime"), dict) else {}
        _assert_runtime_target(runtime_summary, "Team Lead runtime")
        skill_playbooks = team_lead_runtime_payload.get("skillPlaybooks") or []
        if len(skill_playbooks) >= 4:
            ok("Team Lead runtime config records development skill playbooks")
        else:
            fail("Team Lead runtime config missing development skill playbooks", str(skill_playbooks))
    else:
        fail("Team Lead runtime config unreadable", os.path.join(host_ws, "team-lead/stage-summary.json"))

    step("b2. Verify Jira-derived stack constraints propagated into the Team Lead plan")
    plan_payload = _read_json_file(plan_path)
    plan_constraints: dict = {}
    if isinstance(plan_payload, dict):
        plan_constraints = plan_payload.get("tech_stack_constraints") or {}
        if plan_constraints:
            ok("Team Lead plan includes structured tech stack constraints")
        elif expected_constraints:
            fail("Team Lead plan missing tech stack constraints")
        else:
            warn("Team Lead plan has no tech stack constraints (ticket implies no specific stack — OK)")
        _assert_expected_stack_constraints(plan_constraints, expected_constraints, "Team Lead plan")
    elif input_required:
        warn("Team Lead plan not available yet because the workflow is waiting for user clarification")
    else:
        fail("Team Lead plan JSON unreadable", plan_path)

    if input_required:
        step("d. Verify workflow paused before dev dispatch")
        exec_agent_dirs = [
            sub.name
            for sub in Path(host_ws).iterdir()
            if sub.is_dir() and sub.name not in _NON_EXEC_AGENT_DIRS and not (sub / ".git").is_dir()
        ]
        if exec_agent_dirs:
            warn(f"Execution agent artifacts already exist before clarification: {sorted(exec_agent_dirs)}")
        else:
            ok("No execution agent dispatched before required clarification was provided")

        for rel, label in (
            ("compass/command-log.txt", "Compass command log"),
            ("compass/stage-summary.json", "Compass stage summary"),
            ("jira/command-log.txt", "Jira Agent command log"),
            ("jira/stage-summary.json", "Jira Agent stage summary"),
            ("scm/command-log.txt", "SCM Agent command log"),
            ("scm/stage-summary.json", "SCM Agent stage summary"),
        ):
            _ws_file_ok(host_ws, rel, label)
        if DESIGN_URL:
            _ws_file_ok(host_ws, "ui-design/command-log.txt", "UI Design Agent command log")
            _ws_file_ok(host_ws, "ui-design/stage-summary.json", "UI Design Agent stage summary")
        return

    # ── d. Code/repo ──────────────────────────────────────────────────────
    step("d. Verify cloned repo exists in workspace")
    ws_path = Path(host_ws)
    repo_context_payload = _read_json_file(repo_context_path)
    repo_workspace_hint = container_to_host(_extract_repo_workspace_path(repo_context_payload))
    # Primary check: cloned repo directory contains .git
    repo_name = SCM_REPO
    clone_dir = Path(repo_workspace_hint) if repo_workspace_hint else (ws_path / repo_name if repo_name else None)
    git_dirs = list(ws_path.rglob(".git"))
    if clone_dir and (clone_dir / ".git").is_dir():
        ok(f"Cloned repo found: {clone_dir.name}/.git exists")
        # Verify common repo files are present
        repo_files = list(clone_dir.rglob("*"))
        src_files = [f for f in repo_files if f.suffix in (".py", ".js", ".ts", ".kt", ".java", ".md")]
        if src_files:
            ok(f"Repo contains {len(src_files)} source/doc file(s) (e.g. {src_files[0].name})")
        else:
            warn("Cloned repo appears empty (no recognizable source files found)")
    elif git_dirs:
        ok(f"Git repo found in workspace: {git_dirs[0].parent.name}")
    else:
        code_files = (list(ws_path.rglob("*.py")) + list(ws_path.rglob("*.js")) +
                      list(ws_path.rglob("*.ts")) + list(ws_path.rglob("*.kt")))
        if code_files:
            warn(f"No .git found but {len(code_files)} code file(s) present "
                 f"(repo clone may have failed; web agent generated code directly)")
        else:
            fail("Cloned repo NOT found in workspace",
                 f"Expected: {host_ws}/{repo_name}/.git — "
                 "check SCM agent logs for clone errors")

    step("d2. Verify Dev Agent received Jira-derived stack constraints")
    exec_agent_dir = _find_exec_agent_dir(host_ws)
    dev_stage_payload = _read_json_file(os.path.join(host_ws, f"{exec_agent_dir}/stage-summary.json"))
    dev_runtime_payload = dev_stage_payload.get("runtimeConfig") if isinstance(dev_stage_payload, dict) else None
    if isinstance(dev_runtime_payload, dict):
        runtime_summary = dev_runtime_payload.get("runtime") if isinstance(dev_runtime_payload.get("runtime"), dict) else {}
        _assert_runtime_target(runtime_summary, "Dev Agent runtime")
        dev_constraints = dev_runtime_payload.get("techStackConstraints") or {}
        if dev_constraints:
            ok("Dev Agent runtime config includes tech stack constraints")
        elif expected_constraints:
            fail("Dev Agent runtime config missing tech stack constraints")
        else:
            warn("Dev Agent runtime config has no tech stack constraints (OK for docs-only task)")
        skill_playbooks = dev_runtime_payload.get("skillPlaybooks") or []
        if len(skill_playbooks) >= 4:
            ok("Dev Agent runtime config records development skill playbooks")
        elif skill_playbooks:
            warn(f"Dev Agent runtime config has fewer than 4 skill playbooks: {len(skill_playbooks)}")
        else:
            warn("Dev Agent runtime config has no skill playbooks")
        _assert_expected_stack_constraints(dev_constraints, expected_constraints, "Dev Agent runtime config")
        _assert_matching_stack_constraints(plan_constraints, dev_constraints)
    else:
        warn(f"Dev Agent runtime config not available ({exec_agent_dir}/stage-summary.json)")

    _verify_external(j_status_before, j_comments_before, prs_before,
                     branches_before, host_ws, final_state, tid, j_assignee_before)


def _verify_external(j_status_before, j_comments_before, prs_before,
                     branches_before, host_ws, final_state, tid, j_assignee_before):
    exec_agent_dir = _find_exec_agent_dir(host_ws)
    agent_label = exec_agent_dir.replace("-", " ").title()
    # ── e. Jira state changes ─────────────────────────────────────────────
    step("e. Verify Jira ticket state changed and comments added")
    jira_action_events: list[dict] = []
    if host_ws:
        jira_actions_payload = _read_json_file(os.path.join(host_ws, f"{exec_agent_dir}/jira-actions.json"))
        events = jira_actions_payload.get("events") if isinstance(jira_actions_payload, dict) else None
        if isinstance(events, list) and events:
            jira_action_events = [event for event in events if isinstance(event, dict)]
            ok(f"{agent_label} recorded {len(jira_action_events)} Jira action event(s)")
        else:
            fail(f"{agent_label} Jira action evidence missing recorded events")

    fetch_event = _latest_completed_jira_event(jira_action_events, "fetch")
    assign_event = _latest_completed_jira_event(jira_action_events, "assign")
    transition_event = _latest_completed_jira_event(jira_action_events, "transition")
    comment_event = _latest_completed_jira_event(jira_action_events, "comment")

    if fetch_event:
        fail(f"{agent_label} redundantly fetched Jira context instead of using Team Lead handoff")
    else:
        ok(f"{agent_label} did not redundantly fetch Jira context")
    if assign_event:
        ok(f"{agent_label} recorded a completed Jira assign action")
    else:
        fail(f"{agent_label} did not record a completed Jira assign action")
    if transition_event:
        ok(f"{agent_label} recorded a completed Jira transition action")
    else:
        fail(f"{agent_label} did not record a completed Jira transition action")
    if comment_event:
        ok(f"{agent_label} recorded a completed Jira comment action")
    else:
        fail(f"{agent_label} did not record a completed Jira comment action")

    if not JIRA_TOKEN:
        warn("Jira credentials missing -- skipping")
    else:
        j_after = jira_get_issue(JIRA_TICKET_KEY)
        if j_after:
            j_status_after   = j_after.get("fields", {}).get("status", {}).get("name", "")
            assignee_after = j_after.get("fields", {}).get("assignee") or {}
            j_assignee_after = assignee_after.get("accountId", "")
            j_comments_after = j_after.get("fields", {}).get("comment", {}).get("total", 0)
            info(
                "Jira after: "
                f"status={j_status_after!r}, assignee={j_assignee_after or assignee_after.get('displayName', '')!r}, "
                f"comments={j_comments_after}"
            )

            normalized_after = _normalize_text(j_status_after)
            normalized_before = _normalize_text(j_status_before)
            transition_target = str((transition_event or {}).get("targetStatus") or "")
            normalized_target = _normalize_text(transition_target)
            if normalized_target and normalized_after == normalized_target:
                ok(f"Jira status matches recorded transition target: {transition_target!r}")
            elif normalized_after and normalized_after != normalized_before:
                ok(f"Jira status changed: {j_status_before!r} -> {j_status_after!r}")
            else:
                # If status is still a valid workflow state, warn instead of fail.
                # Jira may already have been in the correct terminal state before the run.
                valid_states = {"in review", "under review", "in progress", "done", "resolved"}
                if normalized_after in valid_states:
                    warn(f"Jira status unchanged: {j_status_after!r} (was already in a valid state before the run)")
                else:
                    fail(f"Jira status unchanged: {j_status_after!r}")

            assigned_account = str((assign_event or {}).get("accountId") or "")
            if assigned_account and j_assignee_after == assigned_account:
                ok("Jira assignee matches the recorded assign action")
            elif j_assignee_after and j_assignee_after != j_assignee_before:
                ok("Jira assignee changed during the workflow")
            else:
                # May already have been assigned — warn rather than fail
                warn(
                    f"Jira assignee did not change (may already have been assigned) "
                    f"before={j_assignee_before!r}, after={j_assignee_after!r}, expected={assigned_account!r}"
                )

            new_comments = j_comments_after - j_comments_before
            if new_comments > 0:
                ok(f"Jira has {new_comments} new comment(s)")
                comments = jira_get_comments(JIRA_TICKET_KEY)
                if comments:
                    last = comments[-1]
                    author = last.get("author", {}).get("displayName", "?")
                    body_text = last.get("body", {})
                    if isinstance(body_text, dict):
                        body_text = " ".join(
                            item.get("text", "")
                            for block in body_text.get("content", [])
                            for item in block.get("content", [])
                            if isinstance(item, dict)
                        )
                    info(f"Last comment by {author}: {str(body_text)[:200]}")
            else:
                warn("No new Jira comments added (previous runs may have already commented)")

            expected = {"in progress", "in review", "under review", "done"}
            if j_status_after.lower() in expected:
                ok(f"Jira in expected post-workflow state: {j_status_after!r}")
            else:
                warn(f"Jira state {j_status_after!r} not in expected: {expected}")
        else:
            warn("Could not fetch Jira ticket after test")

    # ── f. Build/test ─────────────────────────────────────────────────────
    step("f. Check for build/test output in workspace")
    if host_ws:
        ws_path = Path(host_ws)
        exec_dirs = [d for d in ws_path.iterdir()
                     if d.is_dir() and d.name not in _NON_EXEC_AGENT_DIRS
                     and not (d / ".git").is_dir()]
        if exec_dirs:
            ok(f"Execution agent workspace directory present: {exec_dirs[0].name}")
        else:
            warn("No execution agent directory in workspace")
    else:
        warn("No workspace path -- cannot check build/test output")

    if host_ws:
        _ws_file_ok(host_ws, f"{exec_agent_dir}/stage-summary.json", f"{agent_label} stage summary")
        _ws_file_ok(host_ws, f"{exec_agent_dir}/command-log.txt", f"{agent_label} command log")
        # test-results.json is only written when tests are run (requires_tests=true);
        # for documentation-only tasks it may legitimately be absent.
        _ws_file_ok_or_warn(host_ws, f"{exec_agent_dir}/test-results.json", f"{agent_label} test results")
        _ws_file_ok(host_ws, f"{exec_agent_dir}/branch-info.json", f"{agent_label} branch info")
        _ws_file_ok(host_ws, f"{exec_agent_dir}/jira-actions.json", f"{agent_label} Jira action evidence")
        _ws_file_ok(host_ws, f"{exec_agent_dir}/pr-evidence.json", f"{agent_label} PR evidence")
        _ws_file_ok(host_ws, "compass/command-log.txt", "Compass command log")
        _ws_file_ok(host_ws, "compass/stage-summary.json", "Compass stage summary")
        _ws_file_ok(host_ws, "jira/command-log.txt", "Jira Agent command log")
        _ws_file_ok(host_ws, "jira/stage-summary.json", "Jira Agent stage summary")
        _ws_file_ok(host_ws, "scm/command-log.txt", "SCM Agent command log")
        _ws_file_ok(host_ws, "scm/stage-summary.json", "SCM Agent stage summary")
        # ui-design artifacts are only written when a design URL is fetched.
        _ws_file_ok_or_warn(host_ws, "ui-design/command-log.txt", "UI Design Agent command log")
        _ws_file_ok_or_warn(host_ws, "ui-design/stage-summary.json", "UI Design Agent stage summary")
        branch_info_path = os.path.join(host_ws, f"{exec_agent_dir}/branch-info.json")
        clone_info_path = os.path.join(host_ws, f"{exec_agent_dir}/clone-info.json")
        test_results_path = os.path.join(host_ws, f"{exec_agent_dir}/test-results.json")
        if os.path.isfile(branch_info_path):
            try:
                branch_info = json.loads(Path(branch_info_path).read_text(encoding="utf-8"))
                if branch_info.get("branch"):
                    ok(f"Branch info recorded local branch: {branch_info['branch']}")
                else:
                    fail("Branch info missing branch name", str(branch_info)[:200])
            except Exception as exc:
                fail("Could not parse branch-info.json", str(exc))
        if os.path.isfile(clone_info_path):
            fail(f"{agent_label} created redundant clone evidence", clone_info_path)
        else:
            ok(f"{agent_label} reused the Team Lead-prepared repo instead of cloning again")
        if os.path.isfile(test_results_path):
            try:
                test_results = json.loads(Path(test_results_path).read_text(encoding="utf-8"))
                attempts = test_results.get("attempts") or []
                if attempts:
                    ok(f"{agent_label} recorded {len(attempts)} build/test attempt(s)")
                else:
                    fail("test-results.json has no attempts", str(test_results)[:200])
            except Exception as exc:
                fail("Could not parse test-results.json", str(exc))

    # ── f2. Local timezone timestamps ────────────────────────────────────
    step("f2. Verify workspace timestamps use the configured local timezone")
    if LOCAL_TIMEZONE:
        info(f"Expected local timezone: {LOCAL_TIMEZONE}")
    else:
        warn("LOCAL_TIMEZONE not configured; only checking for explicit timezone offsets")

    if host_ws:
        _assert_local_timestamp_field(host_ws, "compass/stage-summary.json", "updatedAt", "Compass stage summary")
        _assert_local_timestamp_field(host_ws, "jira/stage-summary.json", "updatedAt", "Jira Agent stage summary")
        _assert_local_timestamp_field(host_ws, "scm/stage-summary.json", "updatedAt", "SCM Agent stage summary")
        # ui-design is optional — only written when a design URL was fetched
        if os.path.isfile(os.path.join(host_ws, "ui-design/stage-summary.json")):
            _assert_local_timestamp_field(host_ws, "ui-design/stage-summary.json", "updatedAt", "UI Design Agent stage summary")
        else:
            warn("UI Design Agent stage summary absent (no design URL in task — skipping timestamp check)")
        _assert_local_timestamp_from_events(host_ws, f"{exec_agent_dir}/jira-actions.json", f"{agent_label} Jira action evidence")
        _assert_local_timestamp_field(host_ws, f"{exec_agent_dir}/pr-evidence.json", "ts", f"{agent_label} PR evidence")

    # ── g. Pull Request ───────────────────────────────────────────────────
    step(f"g. Verify {SCM_PROVIDER_LABEL} Pull Request created")
    if not SCM_TOKEN:
        warn("SCM token missing -- skipping PR verification")
    elif not SCM_OWNER or not SCM_REPO:
        warn("Could not parse project/owner and repo from TEST_GITHUB_REPO_URL")
    else:
        prs_after = scm_prs(SCM_OWNER, SCM_REPO)
        new_numbers = {str(p["number"]) for p in prs_after} - set(prs_before)
        if new_numbers:
            ok(f"New PR(s) created: #{', #'.join(sorted(new_numbers))}")
            for pr in prs_after:
                if str(pr["number"]) in new_numbers:
                    info(f"  PR #{pr['number']}: {pr.get('title')}")
                    info(f"  URL: {pr.get('html_url')}")
                    if pr.get("body"):
                        ok("PR has a description body")
                    else:
                        warn("PR description is empty")
        else:
            branches_after = scm_branches(SCM_OWNER, SCM_REPO)
            new_branches = set(branches_after) - set(branches_before)
            if new_branches:
                ok(f"New branch created: {sorted(new_branches)}")
                warn("PR not yet raised (branch exists; may still be in progress)")
            else:
                warn("No new PRs or branches (workflow may not have reached SCM step)")

    # ── Summary ───────────────────────────────────────────────────────────
    step("Configured Workflow Summary")
    info(f"Task ID:     {tid}")
    info(f"Final state: {final_state}")
    if host_ws:
        n = len(list(Path(host_ws).rglob("*"))) if Path(host_ws).is_dir() else 0
        info(f"Workspace files: {n}")
        if VERBOSE and n:
            for f in sorted(Path(host_ws).rglob("*"))[:40]:
                print(f"       {f.relative_to(host_ws)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_all():
    print(f"\n{Colors.BOLD}{'=' * 64}{Colors.RESET}")
    print(f"{Colors.BOLD}  Constellation -- End-to-End Test Suite{Colors.RESET}")
    print(f"{Colors.BOLD}{'=' * 64}{Colors.RESET}")
    print(f"  Compass:   {COMPASS_URL}")
    print(f"  Registry:  {REGISTRY_URL}")
    print(f"  Artifacts: {HOST_ARTIFACT_ROOT}")
    print(f"  Ticket:    {JIRA_TICKET_URL}")
    print(f"  Repo:      {GITHUB_REPO_URL}")
    print(f"  SCM:       {SCM_PROVIDER_LABEL}")
    print(f"  Timeout:   {WORKFLOW_POLL_TIMEOUT}s")
    print(f"  Local TZ:  {LOCAL_TIMEZONE or '(not configured)'}")
    print(f"  Verbose:   {VERBOSE}")
    print(f"  Time:      {time.strftime('%Y-%m-%d %H:%M:%S')}")

    if WORKFLOW_ONLY:
        if not test_0_prerequisites():
            print(f"\n{Colors.RED}ABORTED: core services not running{Colors.RESET}")
            sys.exit(1)
        test_ticket_full_workflow()
    elif SMOKE_ONLY:
        if not test_0_prerequisites():
            sys.exit(1)
        test_1_agent_card_discovery()
        test_2_registry_state()
    else:
        if not test_0_prerequisites():
            print(f"\n{Colors.RED}ABORTED: core services not running{Colors.RESET}")
            sys.exit(1)
        test_1_agent_card_discovery()
        test_2_registry_state()
        test_3_jira_capability()
        test_4_scm_capability()
        test_5_missing_capability()
        test_6_browser_ui()
        test_7_malformed_request()
        test_ticket_full_workflow()

    total = passed + failed
    print(f"\n{Colors.BOLD}{'=' * 64}{Colors.RESET}")
    if failed == 0:
        print(f"  {Colors.GREEN}{Colors.BOLD}ALL {total} TESTS PASSED{Colors.RESET}")
    else:
        print(f"  {Colors.GREEN}{passed} passed{Colors.RESET}  "
              f"{Colors.RED}{failed} failed{Colors.RESET}  (total: {total})")
        for e in errors:
            print(f"    x {e}")
    print(f"{Colors.BOLD}{'=' * 64}{Colors.RESET}\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    run_all()
