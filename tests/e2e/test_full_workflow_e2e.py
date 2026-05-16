"""Full workflow E2E test: Compass → Team Lead → Web Dev → PR + Jira.

The test sends a development task to Compass and monitors the constellation
system as it drives the full workflow autonomously:
  1. Compass receives request → dispatches to Team Lead
  2. Team Lead fetches Jira ticket (via Jira agent), design (via UIDesign agent),
     clones repo (via SCM agent), analyzes, plans
  3. Team Lead dispatches to Web Dev → implements, tests, creates PR, updates Jira
  4. Team Lead runs Code Review independently
  5. Team Lead reports success → Compass summarizes for user

The test does NOT:
  - Register boundary tools directly
  - Launch agents manually mid-test
  - Orchestrate or interfere with the workflow
It only:
  - Starts constellation agents (which self-register their tools)
  - Sends ONE message to Compass (the task from --task CLI arg)
  - Monitors Team Lead task store for completion
  - Validates workspace artifacts

Configuration (tests/.env — only credentials needed):
  TEST_JIRA_TOKEN, TEST_JIRA_EMAIL            — Jira credentials
  TEST_SCM_TOKEN (or TEST_GITHUB_TOKEN)       — SCM credentials
  TEST_FIGMA_TOKEN                            — Figma token (optional)
  TEST_STITCH_API_KEY                         — Stitch API key (optional)
  TEST_SCM_REPO_URL (or TEST_GITHUB_REPO_URL) — fallback repo URL (optional;
                                                agents discover from Jira ticket)

Run:
    pytest tests/e2e/test_full_workflow_e2e.py -m live -v -s \\
        --task "implement the jira ticket: https://tarch.atlassian.net/browse/CSTL-2"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

# ---------------------------------------------------------------------------
# Config — credentials from tests/.env, task from --task CLI arg
# ---------------------------------------------------------------------------

def _load_test_env() -> dict[str, str]:
    env_file = Path(__file__).parent.parent / ".env"
    env: dict[str, str] = {}
    if not env_file.exists():
        return env
    with open(env_file, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip()
    return env


_TEST_ENV = _load_test_env()


def _env(key: str, default: str = "") -> str:
    return _TEST_ENV.get(key, os.environ.get(key, default))


def _require_env(key: str) -> str:
    val = _env(key)
    if not val:
        pytest.skip(f"Missing required credential: {key} (add to tests/.env)")
    return val


def _extract_jira_info(task_arg: str) -> tuple[str, str]:
    """Extract (jira_ticket_url, jira_key) from task string.

    Handles formats:
      - "implement jira ticket: https://company.atlassian.net/browse/PROJ-123"
      - "https://company.atlassian.net/browse/PROJ-123"
      - "PROJ-123"
    """
    url_match = re.search(
        r"(https?://[^\s]+/browse/([A-Z][A-Z0-9]+-\d+))", task_arg
    )
    if url_match:
        return url_match.group(1), url_match.group(2)
    key_match = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", task_arg)
    if key_match:
        return "", key_match.group(1)
    return "", ""


def _infer_jira_base_url(ticket_url: str) -> str:
    parsed = urlparse(ticket_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _infer_scm_backend(repo_url: str) -> str:
    host = urlparse(repo_url).netloc.lower()
    return "github-rest" if "github.com" in host else "bitbucket"


def _infer_scm_base_url(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _load_live_config(task_arg: str) -> dict:
    """Build test configuration from CLI task arg and tests/.env credentials.

    The task arg (from --task) provides the Jira ticket URL.
    All credentials come from tests/.env.
    SCM repo URL is optional — agents discover it from the Jira ticket if absent.
    """
    jira_ticket_url, jira_key = _extract_jira_info(task_arg)
    if not jira_key:
        pytest.fail(
            f"Cannot extract Jira key from --task argument: {task_arg!r}\n"
            "Expected format: 'implement jira ticket: https://company.atlassian.net/browse/PROJ-123'"
        )

    jira_base_url = _infer_jira_base_url(jira_ticket_url) if jira_ticket_url else ""
    if not jira_base_url:
        # Try to infer from JIRA_BASE_URL env var
        jira_base_url = _env("JIRA_BASE_URL", "https://tarch.atlassian.net")

    # SCM: token required, repo URL optional (agents discover from Jira)
    scm_token = _env("TEST_SCM_TOKEN") or _env("TEST_GITHUB_TOKEN", "")
    scm_repo_url = _env("TEST_SCM_REPO_URL") or _env("TEST_GITHUB_REPO_URL", "")

    # Stitch: extract project_id from URL if provided
    stitch_project_url = _env("TEST_STITCH_PROJECT_URL", "")
    stitch_project_id = ""
    if stitch_project_url and "/projects/" in stitch_project_url:
        stitch_project_id = stitch_project_url.rstrip("/").split("/projects/")[-1].split("/")[0]

    return {
        "task_arg": task_arg,
        "jira_ticket_url": jira_ticket_url,
        "jira_base_url": jira_base_url,
        "jira_key": jira_key,
        "jira_token": _require_env("TEST_JIRA_TOKEN"),
        "jira_email": _require_env("TEST_JIRA_EMAIL"),
        "scm_repo_url": scm_repo_url,  # optional — agents discover from Jira ticket
        "scm_backend": _infer_scm_backend(scm_repo_url) if scm_repo_url else "github-rest",
        "scm_base_url": _infer_scm_base_url(scm_repo_url) if scm_repo_url else "https://github.com",
        "scm_token": scm_token,
        "scm_username": _env("TEST_SCM_USERNAME", ""),
        "figma_url": _env("TEST_FIGMA_FILE_URL", ""),
        "figma_token": _env("TEST_FIGMA_TOKEN", ""),
        "stitch_project_url": stitch_project_url,
        "stitch_project_id": stitch_project_id,
        "stitch_screen_id": _env("TEST_STITCH_SCREEN_ID", ""),
        "stitch_api_key": _env("TEST_STITCH_API_KEY", ""),
        "openai_base_url": _env("OPENAI_BASE_URL", "http://localhost:1288/v1"),
        "openai_model": _env("OPENAI_MODEL", "claude-haiku-4-5-20251001"),
    }


def _set_env_from_config(cfg: dict) -> None:
    """Set process environment variables so agents pick up all credentials."""
    os.environ["OPENAI_BASE_URL"] = cfg["openai_base_url"]
    os.environ["OPENAI_MODEL"] = cfg["openai_model"]
    os.environ["AGENT_RUNTIME"] = "claude-code"
    os.environ.setdefault("OPENAI_API_KEY", "")

    # Jira
    os.environ["JIRA_BASE_URL"] = cfg["jira_base_url"]
    os.environ["JIRA_TOKEN"] = cfg["jira_token"]
    os.environ["JIRA_EMAIL"] = cfg["jira_email"]
    os.environ["JIRA_BACKEND"] = "rest"

    # SCM credentials (always set — repo URL is discovered by agents from Jira)
    if cfg.get("scm_token"):
        os.environ["SCM_TOKEN"] = cfg["scm_token"]
        os.environ["GITHUB_TOKEN"] = cfg["scm_token"]  # also set GITHUB_TOKEN for GitHub REST
    if cfg.get("scm_repo_url"):
        # Set as agent fallback; agents will also try to discover from Jira ticket
        os.environ["SCM_REPO_URL"] = cfg["scm_repo_url"]
        os.environ["SCM_BASE_URL"] = cfg["scm_base_url"]
        os.environ["SCM_BACKEND"] = cfg["scm_backend"]
    else:
        # No explicit repo URL — agents will discover it from Jira ticket
        os.environ["SCM_BASE_URL"] = "https://github.com"
        os.environ["SCM_BACKEND"] = "github-rest"
    if cfg.get("scm_username"):
        os.environ["SCM_USERNAME"] = cfg["scm_username"]

    # Figma
    if cfg.get("figma_token"):
        os.environ["FIGMA_TOKEN"] = cfg["figma_token"]
    if cfg.get("figma_url"):
        os.environ["FIGMA_FILE_URL"] = cfg["figma_url"]

    # Stitch
    if cfg.get("stitch_api_key"):
        os.environ["STITCH_API_KEY"] = cfg["stitch_api_key"]
    if cfg.get("stitch_project_id"):
        os.environ["STITCH_PROJECT_ID"] = cfg["stitch_project_id"]
    if cfg.get("stitch_screen_id"):
        os.environ["STITCH_SCREEN_ID"] = cfg["stitch_screen_id"]


# ---------------------------------------------------------------------------
# Services factory
# ---------------------------------------------------------------------------

def _make_services(runtime=None, task_store=None):
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.runtime.adapter import get_runtime
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore

    effective_runtime = runtime or get_runtime(
        "claude-code",
        model=os.environ.get("OPENAI_MODEL", "claude-haiku-4-5-20251001"),
    )
    return AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=effective_runtime,
        registry_client=None,
        task_store=task_store or InMemoryTaskStore(),
    )


# ---------------------------------------------------------------------------
# Pre-run cleanup
# ---------------------------------------------------------------------------

def _cleanup_for_fresh_run(cfg: dict, workspace_path: str) -> None:
    """Wipe local workspace and delete stale remote feature branches."""
    import shutil
    from urllib.request import Request, urlopen

    if os.path.isdir(workspace_path):
        print(f"[e2e] Cleaning workspace: {workspace_path}")
        shutil.rmtree(workspace_path, ignore_errors=True)
    os.makedirs(workspace_path, exist_ok=True)

    # Only clean remote branches when we know the repo URL upfront
    scm_repo_url = cfg.get("scm_repo_url", "")
    if not scm_repo_url:
        return

    parsed = urlparse(scm_repo_url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(path_parts) < 2 or "github" not in parsed.netloc.lower():
        return
    owner, repo_name = path_parts[0], path_parts[1].rstrip(".git")
    jira_key = cfg.get("jira_key", "")
    if not jira_key:
        return
    jira_prefix = f"feature/{jira_key.lower()}"
    token = cfg.get("scm_token", "")
    if not token:
        return

    try:
        list_req = Request(
            f"https://api.github.com/repos/{owner}/{repo_name}/branches?per_page=100",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"},
        )
        with urlopen(list_req, timeout=15) as resp:
            branches = json.load(resp)
        for b in branches:
            name = b.get("name", "")
            if name.lower().startswith(jira_prefix):
                del_req = Request(
                    f"https://api.github.com/repos/{owner}/{repo_name}/git/refs/heads/{name}",
                    method="DELETE",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"},
                )
                try:
                    with urlopen(del_req, timeout=10):
                        pass
                    print(f"[e2e] Deleted remote branch: {name}")
                except Exception as exc:
                    print(f"[e2e] Could not delete branch {name}: {exc}")
    except Exception as exc:
        print(f"[e2e] Branch cleanup error (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

def _preflight_check_jira(cfg: dict) -> None:
    """Verify Jira credentials and that the target ticket is accessible."""
    from agents.jira.providers.rest import JiraRESTProvider
    provider = JiraRESTProvider(
        base_url=cfg["jira_base_url"],
        token=cfg["jira_token"],
        email=cfg["jira_email"],
        auth_mode="basic",
    )
    myself, status = provider.get_myself()
    assert status == "ok", f"Jira auth failed: {status}"
    print(f"[e2e] Jira auth OK: {myself.get('displayName', '?')}")

    jira_key = cfg.get("jira_key", "")
    if jira_key:
        ticket, status = provider.fetch_issue(jira_key)
        assert status == "ok" and ticket, f"Cannot fetch ticket {jira_key}: {status}"
        print(f"[e2e] Jira ticket OK: {jira_key}")


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.asyncio
async def test_implement_jira_ticket_full_workflow(request):
    """E2E: send task to Compass, constellation system drives the full workflow.

    Pass the development task via --task CLI argument:
        pytest ... --task "implement the jira ticket: https://tarch.atlassian.net/browse/CSTL-2"

    The test only:
      1. Starts all constellation agents (they self-register their tools)
      2. Sends ONE message to Compass (the --task argument)
      3. Monitors Team Lead's task store for completion
      4. Validates workspace artifacts

    Agents drive everything: Jira fetch, design fetch, repo clone, implementation,
    testing, PR creation, code review, Jira update — all inside the system.
    """
    task_arg = request.config.getoption("--task", default="")
    if not task_arg:
        pytest.skip(
            "No --task argument provided.\n"
            "Usage: pytest tests/e2e/test_full_workflow_e2e.py -m live -v -s "
            "--task \"implement the jira ticket: https://tarch.atlassian.net/browse/CSTL-2\""
        )

    cfg = _load_live_config(task_arg)
    _set_env_from_config(cfg)

    artifact_root = os.path.abspath(os.environ.get("ARTIFACT_ROOT", "artifacts"))
    workspace_path = os.path.join(artifact_root, "live-e2e")
    os.makedirs(workspace_path, exist_ok=True)

    # Tell Team Lead's in-process dispatch where to put artifacts
    os.environ["TL_WORKSPACE_PATH"] = workspace_path

    # Log setup
    log_file = os.path.join(artifact_root, "live-e2e-run.log")
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(file_handler)
    sys.stdout.reconfigure(line_buffering=True)

    print("\n" + "=" * 70)
    print(f"[e2e] TASK      : {task_arg}")
    print(f"[e2e] WORKSPACE : {workspace_path}")
    print(f"[e2e] LOG       : {log_file}")
    print(f"[e2e] Jira key  : {cfg['jira_key']}")
    print(f"[e2e] SCM repo  : {cfg.get('scm_repo_url', '(to be discovered from Jira ticket)')}")
    print(f"[e2e] Model     : {cfg['openai_model']}")
    print("=" * 70)

    # ---- Pre-run cleanup ----
    _cleanup_for_fresh_run(cfg, workspace_path)

    # ---- Preflight: Jira credentials ----
    _preflight_check_jira(cfg)

    # ---- Register Compass tools first (idempotent; agents will override dispatch) ----
    from agents.compass.tools import register_compass_tools
    register_compass_tools()

    # =========================================================================
    # Start constellation agents — they self-register their tools into the
    # global ToolRegistry.  The test does NOT register any tools directly.
    # =========================================================================
    print("\n[e2e] Starting constellation agents...")

    # -- Boundary agents --
    from agents.jira.adapter import JiraAgentAdapter, jira_definition
    from agents.scm.adapter import SCMAgentAdapter, scm_definition
    from agents.ui_design.adapter import UIDesignAgentAdapter, ui_design_definition

    jira_agent = JiraAgentAdapter(definition=jira_definition, services=_make_services())
    await jira_agent.start()   # registers: fetch_jira_ticket, jira_transition, jira_comment, …
    print("[e2e] Jira agent started")

    scm_agent = SCMAgentAdapter(definition=scm_definition, services=_make_services())
    await scm_agent.start()    # registers: clone_repo, scm_push, scm_create_pr, scm_list_branches
    print("[e2e] SCM agent started")

    ui_agent = UIDesignAgentAdapter(definition=ui_design_definition, services=_make_services())
    await ui_agent.start()     # registers: fetch_design
    print("[e2e] UI Design agent started")

    # -- Execution agents --
    from agents.code_review.agent import CodeReviewAgent, code_review_definition
    from agents.web_dev.agent import WebDevAgent, web_dev_definition
    from agents.team_lead.agent import TeamLeadAgent, team_lead_definition

    from framework.task_store import InMemoryTaskStore

    cr_agent = CodeReviewAgent(
        definition=code_review_definition,
        services=_make_services(task_store=InMemoryTaskStore()),
    )
    await cr_agent.start()     # registers: dispatch_code_review (in-process)
    print("[e2e] Code Review agent started")

    wd_agent = WebDevAgent(
        definition=web_dev_definition,
        services=_make_services(task_store=InMemoryTaskStore()),
    )
    await wd_agent.start()     # registers: dispatch_web_dev (in-process)
    print("[e2e] Web Dev agent started")

    # Team Lead — use a dedicated task store so the test can poll it
    tl_task_store = InMemoryTaskStore()
    tl_agent = TeamLeadAgent(
        definition=team_lead_definition,
        services=_make_services(task_store=tl_task_store),
    )
    await tl_agent.start()     # registers: dispatch_development_task (in-process, overrides Compass's)
    print("[e2e] Team Lead agent started")

    # ---- Send task to Compass ----
    from agents.compass.agent import CompassAgent, compass_definition

    compass_agent = CompassAgent(
        definition=compass_definition,
        services=_make_services(task_store=InMemoryTaskStore()),
    )

    print(f"\n[e2e] Sending task to Compass:\n  {task_arg}")

    compass_result = await compass_agent.handle_message({
        "message": {
            "messageId": "e2e-full-workflow",
            "role": "ROLE_USER",
            "parts": [{"text": task_arg}],
            "metadata": {"workspacePath": workspace_path},
        }
    })
    compass_state = compass_result["task"]["status"]["state"]
    print(f"[e2e] Compass task state: {compass_state}")

    # ---- Monitor Team Lead (constellation drives the workflow) ----
    print("\n[e2e] Monitoring Team Lead workflow (constellation drives all agents)...")
    print("[e2e] Checkpoints: Jira fetch → design fetch → repo clone → plan → "
          "web dev → code review → PR → report")

    tl_final: dict | None = None
    _TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"}
    deadline = time.monotonic() + 3600  # 1h max
    last_heartbeat = time.monotonic()

    while time.monotonic() < deadline:
        tasks = tl_task_store.list_tasks(agent_id="team-lead")
        for task in tasks:
            if task.status.state.value in _TERMINAL:
                tl_final = tl_task_store.get_task_dict(task.id)
                break
        if tl_final:
            break
        # Progress heartbeat every 30s
        now = time.monotonic()
        if now - last_heartbeat >= 30:
            last_heartbeat = now
            elapsed = int(now - (deadline - 3600))
            tasks_all = tl_task_store.list_tasks(agent_id="team-lead")
            if tasks_all:
                state = tasks_all[0].status.state.value
                print(f"[e2e] Team Lead still running... ({elapsed}s) state={state}")
                _print_workspace_progress(workspace_path)
            else:
                print(f"[e2e] Waiting for Team Lead task to be created... ({elapsed}s)")
        await asyncio.sleep(5.0)

    if not tl_final:
        _dump_workspace_artifacts(workspace_path)
        pytest.fail("[e2e] Team Lead did not reach terminal state within 1h")

    tl_state = tl_final["task"]["status"]["state"]
    tl_meta = tl_final["task"].get("metadata", {})
    actual_workspace = tl_meta.get("workspacePath") or workspace_path

    print(f"\n{'=' * 70}")
    print(f"[e2e] Team Lead final state : {tl_state}")
    print(f"[e2e] Workspace             : {actual_workspace}")
    print("=" * 70)

    if tl_state == "TASK_STATE_FAILED":
        _dump_workspace_artifacts(actual_workspace)
        status_msg = tl_final["task"].get("status", {}).get("message", {})
        parts = status_msg.get("parts", []) if isinstance(status_msg, dict) else []
        error_text = parts[0].get("text", str(status_msg))[:500] if parts else str(status_msg)[:500]
        pytest.fail(f"Team Lead FAILED: {error_text}")

    if tl_state == "TASK_STATE_INPUT_REQUIRED":
        _dump_workspace_artifacts(actual_workspace)
        pytest.fail("Team Lead requires user input — check escalate_to_user path")

    assert tl_state == "TASK_STATE_COMPLETED", f"Expected COMPLETED, got {tl_state}"

    # ---- Validate workspace artifacts ----
    _validate_workspace_artifacts(actual_workspace, cfg["jira_key"])
    print("[e2e] PASSED ✓")


# ---------------------------------------------------------------------------
# Workspace validation
# ---------------------------------------------------------------------------

def _validate_workspace_artifacts(workspace_path: str, jira_key: str) -> None:
    def _check(rel_path: str, required_keys: list[str] | None = None) -> dict:
        full = os.path.join(workspace_path, rel_path)
        if not os.path.isfile(full):
            pytest.fail(f"[workspace] Missing artifact: {rel_path}")
        with open(full, encoding="utf-8") as fh:
            content = json.load(fh)
        data = content.get("data", {})
        if required_keys:
            for key in required_keys:
                assert data.get(key), (
                    f"[workspace] {rel_path}: data.{key} is empty/missing. "
                    f"data={json.dumps({k: str(v)[:100] for k, v in data.items()}, ensure_ascii=False)}"
                )
        return content

    def _check_optional(rel_path: str) -> dict | None:
        full = os.path.join(workspace_path, rel_path)
        if not os.path.isfile(full):
            print(f"[workspace] OPTIONAL artifact missing: {rel_path}")
            return None
        with open(full, encoding="utf-8") as fh:
            return json.load(fh)

    print("\n[e2e] === Workspace Artifact Validation ===")

    # Checkpoint 1: Team Lead gathered Jira ticket
    _check("team_lead/jira-ticket.json")
    print("[workspace] CP-1: Jira ticket ✓")

    # Checkpoint 2: Team Lead gathered design content (optional — may not be a UI task)
    design_spec = _check_optional("team_lead/design-spec.json")
    if design_spec:
        print("[workspace] CP-2: Design spec ✓")
    else:
        print("[workspace] CP-2: Design spec SKIPPED (not a UI task or no design URL)")

    # Checkpoint 3: Team Lead analysis + repo clone
    ctx = _check("team_lead/context-manifest.json")
    repo_cloned = ctx.get("data", {}).get("repo_cloned", False)
    repo_path = ctx.get("data", {}).get("repo_path", "")
    if not repo_cloned:
        # Dump context manifest for debugging
        print(f"[workspace] context-manifest data: {json.dumps(ctx.get('data', {}), indent=2)}")
        pytest.fail(f"[workspace] Repo NOT cloned. context-manifest shows repo_cloned=False")
    print(f"[workspace] CP-3: Repo cloned → {repo_path} ✓")

    # Checkpoint 4: Analysis + delivery plan
    _check("team_lead/analysis.json", ["task_type"])
    _check("team_lead/delivery-plan.json")
    print("[workspace] CP-4: Analysis + delivery plan ✓")

    # Checkpoint 5: Web Dev git setup
    git_log = _check("web-agent/git-setup-log.json")
    assert git_log.get("data", {}).get("repo_exists", False), (
        f"[workspace] web-agent git-setup-log: repo_exists=False. "
        f"data={json.dumps(git_log.get('data', {}))}"
    )
    branch = git_log.get("data", {}).get("branch_name", "")
    print(f"[workspace] CP-5: Git setup ✓ branch={branch}")

    # Checkpoint 6: Web Dev implementation plan + Jira prepare
    _check("web-agent/implementation-plan.json")
    _check("web-agent/jira-prepare-log.json", ["jira_key"])
    print("[workspace] CP-6: Web Dev impl plan + Jira prepare ✓")

    # Checkpoint 8: PR created
    pr_ev = _check("web-agent/pr-evidence.json")
    pr_url = pr_ev.get("data", {}).get("pr_url", "")
    assert pr_url, (
        f"[workspace] pr-evidence.json: pr_url empty. "
        f"data={json.dumps(pr_ev.get('data', {}))}"
    )
    print(f"[workspace] CP-8: PR evidence ✓ pr_url={pr_url}")

    # Checkpoint 8b: Jira updated
    jira_upd = _check("web-agent/jira-update-log.json")
    jira_upd_data = jira_upd.get("data", {})
    print(
        f"[workspace] CP-8b: Jira update "
        f"comment_added={jira_upd_data.get('comment_added')} "
        f"transition_attempted={jira_upd_data.get('transition_attempted')}"
    )

    print("[e2e] === Workspace Validation PASSED ===\n")


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------

def _print_workspace_progress(workspace_path: str) -> None:
    """Print a brief summary of which artifacts exist so far."""
    checkpoints = {
        "CP-1 Jira ticket": "team_lead/jira-ticket.json",
        "CP-2 Design spec": "team_lead/design-spec.json",
        "CP-3 Context manifest": "team_lead/context-manifest.json",
        "CP-4a Analysis": "team_lead/analysis.json",
        "CP-4b Delivery plan": "team_lead/delivery-plan.json",
        "CP-5 Git setup": "web-agent/git-setup-log.json",
        "CP-6a Impl plan": "web-agent/implementation-plan.json",
        "CP-6b Jira prepare": "web-agent/jira-prepare-log.json",
        "CP-8 PR evidence": "web-agent/pr-evidence.json",
        "CP-8b Jira update": "web-agent/jira-update-log.json",
        "CP-9 Review report": "code-review/review-report.json",
    }
    present = []
    missing = []
    for label, path in checkpoints.items():
        if os.path.isfile(os.path.join(workspace_path, path)):
            present.append(label)
        else:
            missing.append(label)
    if present:
        print(f"[e2e] Done: {', '.join(present)}")
    if missing:
        print(f"[e2e] Pending: {', '.join(missing)}")


def _dump_workspace_artifacts(workspace_path: str) -> None:
    print(f"\n[e2e] === Workspace Dump: {workspace_path} ===")
    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules"}]
        for fname in sorted(files):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, workspace_path)
            try:
                with open(fpath, encoding="utf-8") as fh:
                    content = json.load(fh)
                print(f"\n--- {rel} ---")
                print(json.dumps(content, indent=2, ensure_ascii=False)[:800])
            except Exception as exc:
                print(f"\n--- {rel} [READ ERROR: {exc}] ---")
    print("[e2e] === End Workspace Dump ===\n")
