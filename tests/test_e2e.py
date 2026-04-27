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
import sys
import textwrap
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_ENV = PROJECT_ROOT / "tests" / ".env"

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

ARTIFACT_ROOT_HOST = os.environ.get("ARTIFACT_ROOT_HOST", str(PROJECT_ROOT / "artifacts"))

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
    while time.time() < deadline:
        status, body = http_json(f"{COMPASS_URL}/tasks/{tid}", timeout=10)
        if status == 200:
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
    return None


def t_state(body: dict | None) -> str:
    return (body or {}).get("task", {}).get("status", {}).get("state", "")


def t_id(body: dict | None) -> str:
    return (body or {}).get("task", {}).get("id", "")


def t_workspace(body: dict | None) -> str:
    return (body or {}).get("task", {}).get("workspacePath", "")


def t_agent(body: dict | None) -> str:
    return (body or {}).get("task", {}).get("agentId", "")


def container_to_host(container_path: str) -> str:
    if not container_path:
        return ""
    prefix = "/app/artifacts"
    if container_path.startswith(prefix):
        return ARTIFACT_ROOT_HOST + container_path[len(prefix):]
    return container_path


# ---------------------------------------------------------------------------
# Jira API helpers
# ---------------------------------------------------------------------------

def _jira_h() -> dict:
    if not JIRA_TOKEN or not JIRA_EMAIL:
        return {}
    cred = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


def jira_get_issue(key: str) -> dict | None:
    s, b = http_json(f"{JIRA_API_BASE}/issue/{key}?fields=status,assignee,comment",
                     headers=_jira_h())
    return b if s == 200 else None


def jira_get_comments(key: str) -> list:
    s, b = http_json(f"{JIRA_API_BASE}/issue/{key}/comment", headers=_jira_h())
    return b.get("comments", []) if s == 200 else []


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
# Scenario CSTL-1: Full E2E Workflow Validation
# ---------------------------------------------------------------------------

def _ws_file_ok(ws_host: str, rel: str, label: str) -> bool:
    full = os.path.join(ws_host, rel)
    if os.path.isfile(full):
        ok(f"{label} saved ({os.path.getsize(full)} bytes): {rel}")
        return True
    fail(f"{label} not found in workspace", f"expected: {full}")
    return False


def test_cstl1_full_workflow():  # noqa: C901
    section("CSTL-1 Full E2E Workflow Validation")
    print(f"\n  Ticket:  {JIRA_TICKET_URL}")
    print(f"  Repo:    {GITHUB_REPO_URL}")
    print(f"  Timeout: {WORKFLOW_POLL_TIMEOUT}s\n")

    # ── Baseline ──────────────────────────────────────────────────────────
    step("Record Jira state BEFORE test")
    j_before = jira_get_issue(JIRA_TICKET_KEY)
    j_status_before = ""
    j_comments_before = 0
    if j_before:
        j_status_before = j_before.get("fields", {}).get("status", {}).get("name", "")
        j_comments_before = j_before.get("fields", {}).get("comment", {}).get("total", 0)
        info(f"Jira before: status={j_status_before!r}, comments={j_comments_before}")
    else:
        warn("Could not fetch Jira baseline (check TEST_JIRA_TOKEN in tests/.env)")

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
        f" using repository {GITHUB_REPO_URL}"
        f" (React TypeScript, Node.js/Express backend)."
        f" A simple placeholder landing page at / is acceptable:"
        f" header with app name, hero section with welcome message, CTA button to /quiz."
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
        ws_root = Path(ARTIFACT_ROOT_HOST) / "workspaces"
        candidates = sorted(ws_root.glob(f"{tid}*"), reverse=True) if ws_root.is_dir() else []
        if candidates:
            host_ws = str(candidates[0])
            ok(f"Workspace found by task ID: {host_ws}")
        else:
            fail("Workspace not found under artifacts/workspaces/",
                 f"Searched: {ARTIFACT_ROOT_HOST}/workspaces/{tid}*")

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

    # ── d. Code/repo ──────────────────────────────────────────────────────
    step("d. Verify code files or cloned repo in workspace")
    ws_path = Path(host_ws)
    code_files = (list(ws_path.rglob("*.py")) + list(ws_path.rglob("*.js")) +
                  list(ws_path.rglob("*.ts")) + list(ws_path.rglob("*.kt")))
    git_dirs = list(ws_path.rglob(".git"))
    if code_files:
        ok(f"Code files in workspace: {len(code_files)} (e.g. {code_files[0].name})")
    elif git_dirs:
        ok(f"Cloned repo (.git) in workspace")
    else:
        warn("No code files or .git in workspace (web agent may still be running)")

    _verify_external(j_status_before, j_comments_before, prs_before,
                     branches_before, host_ws, final_state, tid)


def _verify_external(j_status_before, j_comments_before, prs_before,
                     branches_before, host_ws, final_state, tid):
    # ── e. Jira state changes ─────────────────────────────────────────────
    step("e. Verify Jira ticket state changed and comments added")
    if not JIRA_TOKEN:
        warn("Jira credentials missing -- skipping")
    else:
        j_after = jira_get_issue(JIRA_TICKET_KEY)
        if j_after:
            j_status_after   = j_after.get("fields", {}).get("status", {}).get("name", "")
            j_comments_after = j_after.get("fields", {}).get("comment", {}).get("total", 0)
            info(f"Jira after: status={j_status_after!r}, comments={j_comments_after}")

            if j_status_after != j_status_before:
                ok(f"Jira status changed: {j_status_before!r} -> {j_status_after!r}")
            else:
                warn(f"Jira status unchanged: {j_status_after!r}")

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
                warn("No new Jira comments added")

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
    print(f"  Artifacts: {ARTIFACT_ROOT_HOST}")
    print(f"  Ticket:    {JIRA_TICKET_URL}")
    print(f"  Repo:      {GITHUB_REPO_URL}")
    print(f"  Timeout:   {WORKFLOW_POLL_TIMEOUT}s")
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
