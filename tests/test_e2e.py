#!/usr/bin/env python3
"""Constellation multi-agent end-to-end validation.

Usage:
  python3 tests/test_e2e.py              # run all tests including CSTL-1 workflow
  python3 tests/test_e2e.py -v           # verbose JSON output
  python3 tests/test_e2e.py --smoke-only # prerequisites + registry only (fast)
  python3 tests/test_e2e.py --cstl-only  # only run the full CSTL-1 workflow test
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
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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
CSTL_ONLY = "--cstl-only" in sys.argv


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


_ENV = _load_env_file(TESTS_ENV)
_COMMON_ENV = _load_env_file(COMMON_ENV)

JIRA_TICKET_URL = _ENV.get("TEST_JIRA_TICKET_URL", "https://tarch.atlassian.net/browse/CSTL-1")
JIRA_TICKET_KEY = JIRA_TICKET_URL.rstrip("/").split("/")[-1]
JIRA_BASE_URL   = "/".join(JIRA_TICKET_URL.split("/")[:3])
JIRA_API_BASE   = f"{JIRA_BASE_URL}/rest/api/3"
JIRA_TOKEN      = _ENV.get("TEST_JIRA_TOKEN", "")
JIRA_EMAIL      = _ENV.get("TEST_JIRA_EMAIL", "")

GITHUB_REPO_URL = _ENV.get("TEST_GITHUB_REPO_URL", "https://github.com/fihtony/english-study-hub")
GITHUB_TOKEN    = _ENV.get("TEST_GITHUB_TOKEN", "")
_gh = GITHUB_REPO_URL.rstrip("/").split("/")
GITHUB_OWNER    = _gh[-2] if len(_gh) >= 2 else ""
GITHUB_REPO     = _gh[-1] if _gh else ""

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


def send_message(text: str, requested_capability: str | None = None, timeout: int = 30):
    payload = {
        "message": {
            "messageId": f"e2e-{int(time.time() * 1000)}",
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
    }
    if requested_capability:
        payload["requestedCapability"] = requested_capability
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
    s, b = http_json(f"{JIRA_URL}/jira/tickets/{key}")
    issue = b.get("issue") if isinstance(b, dict) else None
    return issue if s == 200 and isinstance(issue, dict) else None


def jira_get_comments(key: str) -> list:
    issue = jira_get_issue(key) or {}
    comment_block = issue.get("fields", {}).get("comment") or {}
    comments = comment_block.get("comments")
    return comments if isinstance(comments, list) else []


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _gh_h() -> dict:
    return {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}


def github_prs(owner: str, repo: str, state: str = "open") -> list:
    s, b = http_json(f"https://api.github.com/repos/{owner}/{repo}/pulls?state={state}&per_page=30",
                     headers=_gh_h())
    return b if (s == 200 and isinstance(b, list)) else []


def github_branches(owner: str, repo: str) -> list:
    s, b = http_json(f"https://api.github.com/repos/{owner}/{repo}/branches?per_page=100",
                     headers=_gh_h())
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


def test_cstl1_full_workflow():  # noqa: C901
    section(f"{JIRA_TICKET_KEY} Full E2E Workflow Validation")
    print(f"\n  Ticket:  {JIRA_TICKET_URL}")
    print(f"  Repo:    {GITHUB_REPO_URL}")
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

    step("Record GitHub PRs/branches BEFORE test")
    prs_before: list = []
    branches_before: list = []
    if GITHUB_OWNER and GITHUB_REPO and GITHUB_TOKEN:
        prs_before = [str(p.get("number")) for p in github_prs(GITHUB_OWNER, GITHUB_REPO)]
        branches_before = github_branches(GITHUB_OWNER, GITHUB_REPO)
        info(f"GitHub before: {len(prs_before)} open PRs, {len(branches_before)} branches")
    else:
        warn("GitHub credentials missing -- PR verification skipped")

    # ── a. Submit task ────────────────────────────────────────────────────
    # Build rich request including repo + basic acceptance criteria so team-lead
    # can proceed without asking the user for clarification.
    _request_text = (
        f"implement jira ticket {JIRA_TICKET_URL}"
        f" using repository {GITHUB_REPO_URL}."
        f" Use the Jira ticket and linked design context as the source of truth."
        f" If the repository is sparse, scaffold the required implementation in place instead of switching stacks."
    )
    step(f"a. Submit 'implement jira ticket {JIRA_TICKET_URL}'")
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
    if final_state == "TASK_STATE_COMPLETED":
        ok(f"Task completed (state={final_state})")
    elif final_state == "TASK_STATE_INPUT_REQUIRED":
        warn("Task needs user input -- partial validation follows")
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
    _ws_file_ok(host_ws, "team-lead/plan.json", "Team Lead implementation plan")
    _ws_file_ok(host_ws, "team-lead/stage-summary.json", "Team Lead stage summary")
    _ws_file_ok(host_ws, "team-lead/command-log.txt", "Team Lead command log")
    _ws_file_ok(host_ws, "team-lead/review-notes.json", "Team Lead review notes")

    step("b2. Verify Jira-derived stack constraints propagated into the Team Lead plan")
    plan_payload = _read_json_file(os.path.join(host_ws, "team-lead/plan.json"))
    plan_constraints: dict = {}
    if isinstance(plan_payload, dict):
        plan_constraints = plan_payload.get("tech_stack_constraints") or {}
        if plan_constraints:
            ok("Team Lead plan includes structured tech stack constraints")
        else:
            fail("Team Lead plan missing tech stack constraints")
        _assert_expected_stack_constraints(plan_constraints, expected_constraints, "Team Lead plan")
    else:
        fail("Team Lead plan JSON unreadable", os.path.join(host_ws, "team-lead/plan.json"))

    # ── d. Code/repo ──────────────────────────────────────────────────────
    step("d. Verify cloned repo exists in workspace")
    ws_path = Path(host_ws)
    # Primary check: cloned repo directory contains .git
    repo_name = GITHUB_REPO_URL.rstrip("/").split("/")[-1].rstrip(".git") if GITHUB_REPO_URL else ""
    clone_dir = ws_path / repo_name if repo_name else None
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

    step("d2. Verify Web Agent received Jira-derived stack constraints")
    web_stage_payload = _read_json_file(os.path.join(host_ws, "web-agent/stage-summary.json"))
    web_runtime_payload = web_stage_payload.get("runtimeConfig") if isinstance(web_stage_payload, dict) else None
    if isinstance(web_runtime_payload, dict):
        web_constraints = web_runtime_payload.get("techStackConstraints") or {}
        if web_constraints:
            ok("Web Agent runtime config includes tech stack constraints")
        else:
            fail("Web Agent runtime config missing tech stack constraints")
        _assert_expected_stack_constraints(web_constraints, expected_constraints, "Web Agent runtime config")
        _assert_matching_stack_constraints(plan_constraints, web_constraints)
    else:
        fail("Web Agent runtime config unreadable", os.path.join(host_ws, "web-agent/stage-summary.json"))

    _verify_external(j_status_before, j_comments_before, prs_before,
                     branches_before, host_ws, final_state, tid, j_assignee_before)


def _verify_external(j_status_before, j_comments_before, prs_before,
                     branches_before, host_ws, final_state, tid, j_assignee_before):
    # ── e. Jira state changes ─────────────────────────────────────────────
    step("e. Verify Jira ticket state changed and comments added")
    jira_action_events: list[dict] = []
    if host_ws:
        jira_actions_payload = _read_json_file(os.path.join(host_ws, "web-agent/jira-actions.json"))
        events = jira_actions_payload.get("events") if isinstance(jira_actions_payload, dict) else None
        if isinstance(events, list) and events:
            jira_action_events = [event for event in events if isinstance(event, dict)]
            ok(f"Web Agent recorded {len(jira_action_events)} Jira action event(s)")
        else:
            fail("Web Agent Jira action evidence missing recorded events")

    fetch_event = _latest_completed_jira_event(jira_action_events, "fetch")
    assign_event = _latest_completed_jira_event(jira_action_events, "assign")
    transition_event = _latest_completed_jira_event(jira_action_events, "transition")
    comment_event = _latest_completed_jira_event(jira_action_events, "comment")

    if fetch_event:
        ok("Web Agent recorded a completed Jira fetch action")
    else:
        fail("Web Agent did not record a completed Jira fetch action")
    if assign_event:
        ok("Web Agent recorded a completed Jira assign action")
    else:
        fail("Web Agent did not record a completed Jira assign action")
    if transition_event:
        ok("Web Agent recorded a completed Jira transition action")
    else:
        fail("Web Agent did not record a completed Jira transition action")
    if comment_event:
        ok("Web Agent recorded a completed Jira comment action")
    else:
        fail("Web Agent did not record a completed Jira comment action")

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
                fail(f"Jira status unchanged: {j_status_after!r}")

            assigned_account = str((assign_event or {}).get("accountId") or "")
            if assigned_account and j_assignee_after == assigned_account:
                ok("Jira assignee matches the recorded assign action")
            elif j_assignee_after and j_assignee_after != j_assignee_before:
                ok("Jira assignee changed during the workflow")
            else:
                fail(
                    "Jira assignee did not change to the recorded account",
                    f"before={j_assignee_before!r}, after={j_assignee_after!r}, expected={assigned_account!r}",
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
                fail("No new Jira comments added")

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
        web_dirs = list(ws_path.glob("web-agent*")) + list(ws_path.glob("web_agent*"))
        if web_dirs:
            ok(f"Web-agent workspace directory present: {web_dirs[0].name}")
        else:
            warn("No web-agent directory in workspace")
    else:
        warn("No workspace path -- cannot check build/test output")

    if host_ws:
        _ws_file_ok(host_ws, "web-agent/stage-summary.json", "Web Agent stage summary")
        _ws_file_ok(host_ws, "web-agent/command-log.txt", "Web Agent command log")
        _ws_file_ok(host_ws, "web-agent/test-results.json", "Web Agent test results")
        _ws_file_ok(host_ws, "web-agent/branch-info.json", "Web Agent branch info")
        _ws_file_ok(host_ws, "web-agent/clone-info.json", "Web Agent clone info")
        _ws_file_ok(host_ws, "web-agent/jira-actions.json", "Web Agent Jira action evidence")
        _ws_file_ok(host_ws, "web-agent/pr-evidence.json", "Web Agent PR evidence")
        _ws_file_ok(host_ws, "compass/command-log.txt", "Compass command log")
        _ws_file_ok(host_ws, "compass/stage-summary.json", "Compass stage summary")
        _ws_file_ok(host_ws, "jira/command-log.txt", "Jira Agent command log")
        _ws_file_ok(host_ws, "jira/stage-summary.json", "Jira Agent stage summary")
        _ws_file_ok(host_ws, "scm/command-log.txt", "SCM Agent command log")
        _ws_file_ok(host_ws, "scm/stage-summary.json", "SCM Agent stage summary")
        _ws_file_ok(host_ws, "ui-design/command-log.txt", "UI Design Agent command log")
        _ws_file_ok(host_ws, "ui-design/stage-summary.json", "UI Design Agent stage summary")
        branch_info_path = os.path.join(host_ws, "web-agent/branch-info.json")
        test_results_path = os.path.join(host_ws, "web-agent/test-results.json")
        if os.path.isfile(branch_info_path):
            try:
                branch_info = json.loads(Path(branch_info_path).read_text(encoding="utf-8"))
                if branch_info.get("branch"):
                    ok(f"Branch info recorded local branch: {branch_info['branch']}")
                else:
                    fail("Branch info missing branch name", str(branch_info)[:200])
            except Exception as exc:
                fail("Could not parse branch-info.json", str(exc))
        if os.path.isfile(test_results_path):
            try:
                test_results = json.loads(Path(test_results_path).read_text(encoding="utf-8"))
                attempts = test_results.get("attempts") or []
                if attempts:
                    ok(f"Web Agent recorded {len(attempts)} build/test attempt(s)")
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
        _assert_local_timestamp_field(host_ws, "ui-design/stage-summary.json", "updatedAt", "UI Design Agent stage summary")
        _assert_local_timestamp_from_events(host_ws, "web-agent/jira-actions.json", "Web Agent Jira action evidence")
        _assert_local_timestamp_field(host_ws, "web-agent/pr-evidence.json", "ts", "Web Agent PR evidence")

    # ── g. Pull Request ───────────────────────────────────────────────────
    step("g. Verify GitHub Pull Request created")
    if not GITHUB_TOKEN:
        warn("GitHub token missing -- skipping PR verification")
    elif not GITHUB_OWNER:
        warn("Could not parse owner/repo from GITHUB_REPO_URL")
    else:
        prs_after = github_prs(GITHUB_OWNER, GITHUB_REPO)
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
            branches_after = github_branches(GITHUB_OWNER, GITHUB_REPO)
            new_branches = set(branches_after) - set(branches_before)
            if new_branches:
                ok(f"New branch created: {sorted(new_branches)}")
                warn("PR not yet raised (branch exists; may still be in progress)")
            else:
                warn("No new PRs or branches (workflow may not have reached SCM step)")

    # ── Summary ───────────────────────────────────────────────────────────
    step("CSTL-1 Summary")
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
    print(f"  Timeout:   {WORKFLOW_POLL_TIMEOUT}s")
    print(f"  Local TZ:  {LOCAL_TIMEZONE or '(not configured)'}")
    print(f"  Verbose:   {VERBOSE}")
    print(f"  Time:      {time.strftime('%Y-%m-%d %H:%M:%S')}")

    if CSTL_ONLY:
        if not test_0_prerequisites():
            print(f"\n{Colors.RED}ABORTED: core services not running{Colors.RESET}")
            sys.exit(1)
        test_cstl1_full_workflow()
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
        test_cstl1_full_workflow()

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
