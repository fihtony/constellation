"""Full workflow E2E test: Compass → Team Lead → Web Dev → PR + Jira.

Single focused test that exercises the REAL multi-agent chain:
  1. Compass receives user request and dispatches to Team Lead
  2. Team Lead analyzes, gathers Jira context, plans, dispatches to Web Dev
  3. Web Dev implements changes, creates branch + PR, updates Jira
  4. Code Review auto-approves (stub)
  5. Team Lead reports success

Requirements:
  - Jira (Atlassian Cloud) with a valid token
  - SCM (GitHub) with a valid token
  - LLM (CopilotConnect) reachable at OPENAI_BASE_URL

Run:
    pytest tests/e2e/test_full_workflow_e2e.py -m live -v -s
"""
from __future__ import annotations

import json
import os
import queue
import time
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest

# ---------------------------------------------------------------------------
# Config — all values from tests/.env, zero PII in code
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
        pytest.skip(f"Missing required env var: {key}")
    return val


def _extract_jira_key(ticket_url: str) -> str:
    parts = urlparse(ticket_url).path.rstrip("/").split("/")
    return parts[-1] if parts else ""


def _infer_jira_base_url(ticket_url: str) -> str:
    parsed = urlparse(ticket_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _infer_scm_backend(repo_url: str) -> str:
    host = urlparse(repo_url).netloc.lower()
    return "github-rest" if "github.com" in host else "bitbucket"


def _infer_scm_base_url(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _load_live_config() -> dict:
    """Load and validate live E2E config from tests/.env."""
    jira_ticket_url = _require_env("TEST_JIRA_TICKET_URL")
    # Prefer TEST_SCM_REPO_URL (canonical SCM URL), fall back to TEST_GITHUB_REPO_URL
    scm_repo_url = _env("TEST_SCM_REPO_URL") or _require_env("TEST_GITHUB_REPO_URL")
    # Prefer TEST_SCM_TOKEN (Bitbucket / non-GitHub PAT), fall back to TEST_GITHUB_TOKEN
    scm_token = _env("TEST_SCM_TOKEN") or _require_env("TEST_GITHUB_TOKEN")
    return {
        "jira_ticket_url": jira_ticket_url,
        "jira_base_url": _infer_jira_base_url(jira_ticket_url),
        "jira_key": _extract_jira_key(jira_ticket_url),
        "jira_token": _require_env("TEST_JIRA_TOKEN"),
        "jira_email": _require_env("TEST_JIRA_EMAIL"),
        "scm_repo_url": scm_repo_url,
        "scm_backend": _infer_scm_backend(scm_repo_url),
        "scm_base_url": _infer_scm_base_url(scm_repo_url),
        "scm_token": scm_token,
        # SCM_USERNAME is optional; required for Bitbucket Basic auth, omit for PAT Bearer auth
        "scm_username": _env("TEST_SCM_USERNAME", ""),
        "figma_url": _env("TEST_FIGMA_FILE_URL", ""),
        "figma_token": _env("TEST_FIGMA_TOKEN", ""),
        "openai_base_url": _require_env("OPENAI_BASE_URL"),
        "openai_model": _env("OPENAI_MODEL", "gpt-5-mini"),
    }


def _set_env_from_config(cfg: dict) -> None:
    """Populate env vars so agents can discover services."""
    os.environ["OPENAI_BASE_URL"] = cfg["openai_base_url"]
    os.environ["OPENAI_MODEL"] = cfg["openai_model"]
    os.environ["AGENT_RUNTIME"] = "connect-agent"
    os.environ.setdefault("OPENAI_API_KEY", "")
    os.environ.setdefault("ARTIFACT_ROOT", "artifacts/")
    os.environ["JIRA_BASE_URL"] = cfg["jira_base_url"]
    os.environ["JIRA_TOKEN"] = cfg["jira_token"]
    os.environ["JIRA_EMAIL"] = cfg["jira_email"]
    os.environ["JIRA_BACKEND"] = "rest"
    os.environ["SCM_BASE_URL"] = cfg["scm_base_url"]
    os.environ["SCM_TOKEN"] = cfg["scm_token"]
    os.environ["SCM_BACKEND"] = cfg["scm_backend"]
    # SCM_USERNAME: set only when provided (Bearer PAT auth needs it unset)
    if cfg.get("scm_username"):
        os.environ["SCM_USERNAME"] = cfg["scm_username"]
    elif "SCM_USERNAME" in os.environ:
        del os.environ["SCM_USERNAME"]  # ensure clean PAT Bearer auth
    if cfg.get("figma_token"):
        os.environ["FIGMA_TOKEN"] = cfg["figma_token"]


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
        "connect-agent",
        model=os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
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
# Live boundary tool registration — in-process, no Docker
# ---------------------------------------------------------------------------

def _register_live_boundary_tools(cfg: dict) -> None:
    """Register live Jira/SCM tools so tests run without Docker services."""
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry
    from agents.jira.providers.rest import JiraRESTProvider

    registry = get_registry()
    jira_provider = JiraRESTProvider(
        base_url=cfg["jira_base_url"],
        token=cfg["jira_token"],
        email=cfg["jira_email"],
        auth_mode="basic",
    )

    class _LiveFetchJiraTicket(BaseTool):
        name = "fetch_jira_ticket"
        description = "Fetch a Jira ticket (live provider)."
        parameters_schema = {"type": "object", "properties": {"ticket_key": {"type": "string"}}, "required": ["ticket_key"]}
        def execute_sync(self, ticket_key: str = "") -> ToolResult:
            data, status = jira_provider.fetch_issue(ticket_key)
            return ToolResult(output=json.dumps({"ticket": data, "status": status}))

    class _LiveCloneRepo(BaseTool):
        name = "clone_repo"
        description = "Clone a repository (live SCM)."
        parameters_schema = {"type": "object", "properties": {"repo_url": {"type": "string"}, "target_path": {"type": "string"}}, "required": ["repo_url", "target_path"]}
        def execute_sync(self, repo_url: str = "", target_path: str = "") -> ToolResult:
            from agents.scm.adapter import SCMAgentAdapter, scm_definition
            adapter = SCMAgentAdapter(definition=scm_definition, services=_make_services())
            result = adapter._dispatch("scm.repo.clone", "", {
                "metadata": {"repoUrl": repo_url, "targetPath": target_path, "token": cfg["scm_token"]}
            })
            return ToolResult(output=json.dumps(result))

    class _MockFetchDesign(BaseTool):
        name = "fetch_design"
        description = "Fetch design context (no-op for non-UI tasks)."
        parameters_schema = {"type": "object", "properties": {}, "required": []}
        def execute_sync(self, **kw) -> ToolResult:
            return ToolResult(output=json.dumps({}))

    class _MockDispatchCodeReview(BaseTool):
        name = "dispatch_code_review"
        description = "Auto-approve code review (live E2E stub)."
        parameters_schema = {"type": "object", "properties": {}, "required": []}
        def execute_sync(self, **kw) -> ToolResult:
            return ToolResult(output=json.dumps({"verdict": "approved", "summary": "Auto-approved for live E2E test."}))

    for tool in (_LiveFetchJiraTicket(), _LiveCloneRepo(), _MockFetchDesign(), _MockDispatchCodeReview()):
        registry.register(tool)


def _register_live_jira_scm_tools(cfg: dict) -> None:
    """Register in-process Jira/SCM tools for Web Dev agent."""
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry
    from agents.jira.providers.rest import JiraRESTProvider

    registry = get_registry()
    jira_provider = JiraRESTProvider(
        base_url=cfg["jira_base_url"],
        token=cfg["jira_token"],
        email=cfg["jira_email"],
        auth_mode="basic",
    )

    class _LiveJiraTransition(BaseTool):
        name = "jira_transition"
        description = "Transition Jira ticket (live)."
        parameters_schema = {"type": "object", "properties": {"ticket_key": {"type": "string"}, "transition_name": {"type": "string"}}, "required": ["ticket_key", "transition_name"]}
        def execute_sync(self, ticket_key: str = "", transition_name: str = "") -> ToolResult:
            data, status = jira_provider.transition_issue(ticket_key, transition_name)
            return ToolResult(output=json.dumps({"transitionId": data, "status": status}))

    class _LiveJiraComment(BaseTool):
        name = "jira_comment"
        description = "Add Jira comment (live)."
        parameters_schema = {"type": "object", "properties": {"ticket_key": {"type": "string"}, "comment": {"type": "string"}}, "required": ["ticket_key", "comment"]}
        def execute_sync(self, ticket_key: str = "", comment: str = "") -> ToolResult:
            data, status = jira_provider.add_comment(ticket_key, comment)
            return ToolResult(output=json.dumps({"comment": data, "status": status}))

    class _LiveJiraUpdate(BaseTool):
        name = "jira_update"
        description = "Update Jira ticket fields (live)."
        parameters_schema = {"type": "object", "properties": {"ticket_key": {"type": "string"}, "fields": {"type": "object"}}, "required": ["ticket_key"]}
        def execute_sync(self, ticket_key: str = "", fields: dict | None = None) -> ToolResult:
            data, status = jira_provider.update_issue_fields(ticket_key, fields or {})
            return ToolResult(output=json.dumps({"result": data, "status": status}))

    class _LiveJiraListTransitions(BaseTool):
        name = "jira_list_transitions"
        description = "List Jira transitions (live)."
        parameters_schema = {"type": "object", "properties": {"ticket_key": {"type": "string"}}, "required": ["ticket_key"]}
        def execute_sync(self, ticket_key: str = "") -> ToolResult:
            data, status = jira_provider.get_transitions(ticket_key)
            names = [t.get("name") for t in data if isinstance(t, dict)]
            print(f"[live-jira] get_transitions({ticket_key}): status={status}, count={len(data)}, names={names}")
            return ToolResult(output=json.dumps({"transitions": data, "status": status}))

    class _LiveJiraGetTokenUser(BaseTool):
        name = "jira_get_token_user"
        description = "Get Jira token user (live)."
        parameters_schema = {"type": "object", "properties": {}, "required": []}
        def execute_sync(self) -> ToolResult:
            data, status = jira_provider.get_myself()
            return ToolResult(output=json.dumps({"user": data, "status": status}))

    class _LiveJiraListComments(BaseTool):
        name = "jira_list_comments"
        description = "List Jira comments (live)."
        parameters_schema = {"type": "object", "properties": {"ticket_key": {"type": "string"}}, "required": ["ticket_key"]}
        def execute_sync(self, ticket_key: str = "") -> ToolResult:
            data, status = jira_provider.list_comments(ticket_key)
            return ToolResult(output=json.dumps({"comments": data, "status": status}))

    class _LiveSCMPush(BaseTool):
        name = "scm_push"
        description = "Push branch to remote (live SCM)."
        parameters_schema = {"type": "object", "properties": {"repo_path": {"type": "string"}, "branch": {"type": "string"}}, "required": ["repo_path", "branch"]}
        def execute_sync(self, repo_path: str = "", branch: str = "") -> ToolResult:
            from agents.scm.adapter import SCMAgentAdapter, scm_definition
            adapter = SCMAgentAdapter(definition=scm_definition, services=_make_services())
            result = adapter._dispatch("scm.branch.push", "", {
                "metadata": {"repoPath": repo_path, "branch": branch, "token": cfg["scm_token"]}
            })
            return ToolResult(output=json.dumps(result))

    class _LiveSCMCreatePR(BaseTool):
        name = "scm_create_pr"
        description = "Create PR (live SCM)."
        parameters_schema = {"type": "object", "properties": {"repo_url": {"type": "string"}, "source_branch": {"type": "string"}, "target_branch": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}}, "required": ["repo_url", "source_branch", "title", "description"]}
        def execute_sync(self, repo_url: str = "", source_branch: str = "", target_branch: str = "main", title: str = "", description: str = "") -> ToolResult:
            from agents.scm.adapter import SCMAgentAdapter, scm_definition
            from agents.web_dev.tools import _parse_repo_coordinates
            adapter = SCMAgentAdapter(definition=scm_definition, services=_make_services())
            project, repo = _parse_repo_coordinates(repo_url)
            result = adapter._dispatch("scm.pr.create", title, {
                "metadata": {
                    "project": project, "repo": repo,
                    "sourceBranch": source_branch, "targetBranch": target_branch,
                    "title": title, "description": description,
                    "token": cfg["scm_token"],
                }
            })
            return ToolResult(output=json.dumps(result))

    for tool in (_LiveJiraTransition(), _LiveJiraComment(), _LiveJiraUpdate(),
                 _LiveJiraListTransitions(), _LiveJiraGetTokenUser(), _LiveJiraListComments(),
                 _LiveSCMPush(), _LiveSCMCreatePR()):
        registry.register(tool)


def _register_live_dispatch_web_dev(cfg: dict) -> None:
    """Register dispatch_web_dev that runs Web Dev agent in-process."""
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry

    registry = get_registry()

    class _LiveDispatchWebDev(BaseTool):
        name = "dispatch_web_dev"
        description = "Run web dev agent in-process."
        parameters_schema = {"type": "object", "properties": {}, "required": []}

        def execute_sync(self, task_description: str = "", jira_context=None, design_context=None,
                         repo_url: str = "", repo_path: str = "", workspace_path: str = "",
                         context_manifest_path: str = "", jira_files=None, design_files=None,
                         revision_feedback: str = "", definition_of_done=None) -> ToolResult:
            import asyncio as _asyncio
            import concurrent.futures

            def _run_web_dev():
                loop = _asyncio.new_event_loop()
                _asyncio.set_event_loop(loop)
                try:
                    from framework.task_store import InMemoryTaskStore
                    from agents.web_dev.agent import WebDevAgent, web_dev_definition

                    wd_task_store = InMemoryTaskStore()
                    wd_services = _make_services(task_store=wd_task_store)
                    agent = WebDevAgent(definition=web_dev_definition, services=wd_services)
                    loop.run_until_complete(agent.start())

                    # Re-register live tools in this thread's context
                    _register_live_jira_scm_tools(cfg)

                    msg = {
                        "message": {
                            "messageId": "inline-web-dev",
                            "role": "ROLE_USER",
                            "parts": [{"text": task_description}],
                            "metadata": {
                                "jiraContext": jira_context or {},
                                "designContext": design_context,
                                "repoUrl": repo_url,
                                "repoPath": repo_path,
                                "workspacePath": workspace_path,
                                "contextManifestPath": context_manifest_path,
                                "jiraFiles": jira_files or [],
                                "designFiles": design_files or [],
                                "revisionFeedback": revision_feedback,
                                "definitionOfDone": definition_of_done or {},
                            },
                        }
                    }
                    result = loop.run_until_complete(agent.handle_message(msg))
                    task_id = result["task"]["id"]
                    print(f"[live-e2e] Web Dev task started: {task_id}")

                    # Poll until terminal state
                    deadline = time.monotonic() + 1800
                    while time.monotonic() < deadline:
                        td = wd_task_store.get_task_dict(task_id)
                        state = td["task"]["status"]["state"]
                        if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"):
                            break
                        time.sleep(2.0)

                    final_td = wd_task_store.get_task_dict(task_id)
                    final_state = final_td["task"]["status"]["state"]
                    arts = final_td["task"].get("artifacts", [])
                    print(f"[live-e2e] Web Dev final state: {final_state}, artifacts: {len(arts)}")

                    # Extract evidence from artifacts
                    pr_url = ""
                    branch = ""
                    jira_in_review = False
                    for art in arts:
                        m = art.get("metadata", {})
                        pr_url = pr_url or m.get("prUrl", "")
                        branch = branch or m.get("branch", "")
                        jir = m.get("jiraInReview")
                        if jir:
                            jira_in_review = jir in (True, "True", "true", "1")

                    summary = (arts[0].get("parts", [{}])[0].get("text", "Done.") if arts else "Done.")
                    print(f"[live-e2e] Web Dev result: prUrl={pr_url!r} branch={branch!r} jiraInReview={jira_in_review}")
                    return {
                        "status": "completed" if final_state == "TASK_STATE_COMPLETED" else "error",
                        "summary": summary,
                        "prUrl": pr_url,
                        "branch": branch,
                        "jiraInReview": jira_in_review,
                    }
                except Exception as exc:
                    import traceback
                    print(f"[live-e2e] Web Dev exception: {exc}")
                    traceback.print_exc()
                    return {"status": "error", "summary": str(exc), "prUrl": "", "branch": "", "jiraInReview": False}
                finally:
                    loop.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_run_web_dev)
                result_dict = future.result(timeout=960)
            return ToolResult(output=json.dumps(result_dict))

    registry.register(_LiveDispatchWebDev())


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.asyncio
async def test_implement_jira_ticket_full_workflow():
    """Full workflow: Compass → Team Lead → Web Dev → PR + Jira update.

    Single focused test that verifies the happy path per design doc §10.3.
    All boundary agents run in-process (no Docker required).

    Expected outcome:
      - PR is created in the target repo
      - Jira ticket is transitioned to "In Review"
      - Jira comment is added with PR URL
    """
    cfg = _load_live_config()
    _set_env_from_config(cfg)

    # Deterministic workspace path under artifacts/
    workspace_path = os.path.join(
        os.path.abspath(os.environ.get("ARTIFACT_ROOT", "artifacts")),
        "live-e2e",
    )
    os.makedirs(workspace_path, exist_ok=True)

    print("\n" + "=" * 70)
    print(f"[live-e2e] WORKSPACE: {workspace_path}")
    print(f"[live-e2e] Jira key: {cfg['jira_key']}")
    print(f"[live-e2e] SCM backend: {cfg['scm_backend']}")
    print(f"[live-e2e] Model: {cfg['openai_model']}")
    print("=" * 70)

    # ---- Preflight: verify Jira credentials ----
    from agents.jira.providers.rest import JiraRESTProvider
    jira_provider = JiraRESTProvider(
        base_url=cfg["jira_base_url"],
        token=cfg["jira_token"],
        email=cfg["jira_email"],
        auth_mode="basic",
    )
    myself, jira_auth_status = jira_provider.get_myself()
    assert jira_auth_status == "ok", f"Jira auth failed: {jira_auth_status}"
    print(f"[live-e2e] Jira auth OK: {myself.get('displayName', '?')}")

    ticket, ticket_status = jira_provider.fetch_issue(cfg["jira_key"])
    assert ticket_status == "ok" and ticket, f"Cannot fetch ticket {cfg['jira_key']}: {ticket_status}"
    print(f"[live-e2e] Ticket fetched: {cfg['jira_key']}")

    # ---- Preflight: verify SCM clone ----
    import tempfile, shutil
    _preflight_clone_dir = os.path.join(tempfile.gettempdir(), "constellation-preflight-clone")
    if os.path.isdir(_preflight_clone_dir):
        shutil.rmtree(_preflight_clone_dir, ignore_errors=True)
    from agents.scm.adapter import SCMAgentAdapter, scm_definition
    _scm_svc = _make_services()
    _scm_adapter = SCMAgentAdapter(definition=scm_definition, services=_scm_svc)
    _clone_result = _scm_adapter._dispatch(
        "scm.repo.clone", "",
        {"metadata": {"repoUrl": cfg["scm_repo_url"], "targetPath": _preflight_clone_dir}},
    )
    if _clone_result.get("error") or not os.path.isdir(_preflight_clone_dir):
        detail = _clone_result.get("detail", "")
        pytest.fail(
            f"[live-e2e] SCM preflight clone FAILED: {_clone_result.get('error', 'path missing')} "
            f"| git: {detail}\n"
            f"Hint: set TEST_SCM_USERNAME=<your-bitbucket-username> in tests/.env if PAT Bearer fails"
        )
    shutil.rmtree(_preflight_clone_dir, ignore_errors=True)
    print(f"[live-e2e] SCM preflight clone OK: {cfg['scm_repo_url']}")

    # ---- Register all live tools ----
    _register_live_jira_scm_tools(cfg)
    _register_live_boundary_tools(cfg)
    _register_live_dispatch_web_dev(cfg)

    # ---- Set up Team Lead result queue ----
    tl_result_queue: queue.Queue = queue.Queue()

    # Override Compass's dispatch_development_task to run Team Lead in-process
    _register_compass_dispatch(cfg, workspace_path, tl_result_queue)

    # ---- Start Compass ----
    from agents.compass.agent import CompassAgent, compass_definition
    from framework.task_store import InMemoryTaskStore

    compass_task_store = InMemoryTaskStore()
    compass_services = _make_services(task_store=compass_task_store)
    compass_agent = CompassAgent(definition=compass_definition, services=compass_services)

    user_message = (
        f"Please implement Jira ticket {cfg['jira_key']} "
        f"in the repository {cfg['scm_repo_url']}. "
        f"Ticket URL: {cfg['jira_ticket_url']}"
    )
    message = {
        "message": {
            "messageId": "live-e2e-full-workflow",
            "role": "ROLE_USER",
            "parts": [{"text": user_message}],
            "metadata": {"workspacePath": workspace_path},
        },
    }

    print(f"[live-e2e] Sending request to Compass...")
    compass_result = await compass_agent.handle_message(message)
    compass_state = compass_result["task"]["status"]["state"]
    print(f"[live-e2e] Compass finished: {compass_state}")

    assert compass_state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"), \
        f"Compass did not reach terminal state: {compass_state}"

    # ---- Wait for Team Lead ----
    print("[live-e2e] Waiting for Team Lead to complete...")
    try:
        final = tl_result_queue.get(timeout=2400)
    except queue.Empty:
        pytest.fail("[live-e2e] Team Lead did not complete within 2400s")
    if "error" in final and "task" not in final:
        pytest.fail(f"[live-e2e] Team Lead thread crashed: {final['error']}")

    task_state = final["task"]["status"]["state"]
    artifacts = final["task"].get("artifacts", [])

    print(f"\n{'=' * 70}")
    print(f"[live-e2e] RESULT: state={task_state}, artifacts={len(artifacts)}")

    if task_state == "TASK_STATE_FAILED":
        status_msg = final["task"].get("status", {}).get("message", {})
        if isinstance(status_msg, dict):
            parts = status_msg.get("parts", [])
            error_text = parts[0].get("text", "") if parts else str(status_msg)
        else:
            error_text = str(status_msg)
        print(f"[live-e2e] FAILURE REASON: {error_text[:500]}")
        # Dump workspace artifacts for diagnosis
        _dump_workspace_artifacts(workspace_path)
        pytest.fail(f"Team Lead FAILED: {error_text[:500]}")

    assert task_state == "TASK_STATE_COMPLETED", f"Expected COMPLETED, got {task_state}"
    assert len(artifacts) > 0, "No artifacts returned"

    meta = artifacts[0].get("metadata", {})
    pr_url = meta.get("prUrl", "")
    branch = meta.get("branch", "")
    jira_in_review = meta.get("jiraInReview", False)

    print(f"[live-e2e] PR URL: {pr_url}")
    print(f"[live-e2e] Branch: {branch}")
    print(f"[live-e2e] Jira In Review: {jira_in_review}")
    print(f"[live-e2e] Workspace: {workspace_path}")
    print("=" * 70)

    # ---- Step-level workspace validation ----
    _validate_workspace_artifacts(workspace_path, cfg["jira_key"])

    assert pr_url, f"prUrl missing from artifact metadata: {meta}"
    assert branch, f"branch missing from artifact metadata: {meta}"
    assert jira_in_review, (
        f"Jira ticket was NOT transitioned to 'In Review'. "
        f"Check {workspace_path}/web-agent/jira-update-log.json for details."
    )
    print("[live-e2e] PASSED")


# ---------------------------------------------------------------------------
# Workspace validation helpers
# ---------------------------------------------------------------------------

def _validate_workspace_artifacts(workspace_path: str, jira_key: str) -> None:
    """Assert expected workspace artifacts exist and contain data.

    Fails fast with a clear message when a required artifact is missing,
    so the developer knows exactly which step failed.
    """
    def _check_artifact(rel_path: str, required_data_keys: "list[str] | None" = None) -> dict:
        full_path = os.path.join(workspace_path, rel_path)
        if not os.path.isfile(full_path):
            pytest.fail(f"[workspace-check] Missing artifact: {rel_path}")
        with open(full_path, encoding="utf-8") as fh:
            content = json.load(fh)
        data = content.get("data", {})
        if required_data_keys:
            for key in required_data_keys:
                assert data.get(key), (
                    f"[workspace-check] {rel_path}: data.{key} is empty/missing. "
                    f"data={json.dumps({k: str(v)[:100] for k, v in data.items()}, ensure_ascii=False)}"
                )
        return content

    print("\n[live-e2e] === Workspace Artifact Validation ===")

    # Team Lead artifacts
    _check_artifact("team_lead/jira-ticket.json")
    _check_artifact("team_lead/analysis.json", ["task_type"])
    _check_artifact("team_lead/delivery-plan.json")
    ctx = _check_artifact("team_lead/context-manifest.json")
    repo_cloned = ctx.get("data", {}).get("repo_cloned", False)
    assert repo_cloned, (
        f"[workspace-check] Repo was NOT cloned. "
        f"context-manifest.json: {json.dumps(ctx.get('data', {}), indent=2)}"
    )
    repo_path = ctx.get("data", {}).get("repo_path", "")
    print(f"[workspace-check] Team Lead artifacts: OK  (repo_cloned={repo_cloned}, repo_path={repo_path})")

    # Web Dev artifacts
    git_log = _check_artifact("web-agent/git-setup-log.json")
    assert git_log.get("data", {}).get("repo_exists", False), (
        f"[workspace-check] web-agent git-setup-log: repo_exists=False. "
        f"data={json.dumps(git_log.get('data', {}))}"
    )
    branch_name = git_log.get("data", {}).get("branch_name", "")
    print(f"[workspace-check] Web Dev git setup: OK  (branch={branch_name})")

    _check_artifact("web-agent/implementation-plan.json")
    _check_artifact("web-agent/jira-prepare-log.json", ["jira_key"])

    # PR evidence
    pr_ev = _check_artifact("web-agent/pr-evidence.json")
    pr_url_evidence = pr_ev.get("data", {}).get("pr_url", "")
    assert pr_url_evidence, (
        f"[workspace-check] pr-evidence.json: pr_url is empty. "
        f"data={json.dumps(pr_ev.get('data', {}))}"
    )
    print(f"[workspace-check] PR evidence: OK  (pr_url={pr_url_evidence})")

    # Jira update log
    jira_upd = _check_artifact("web-agent/jira-update-log.json")
    jira_upd_data = jira_upd.get("data", {})
    print(
        f"[workspace-check] Jira update: comment_added={jira_upd_data.get('comment_added')} "
        f"transition_attempted={jira_upd_data.get('transition_attempted')}"
    )

    print("[live-e2e] === Workspace Validation PASSED ===\n")


def _dump_workspace_artifacts(workspace_path: str) -> None:
    """Print all workspace artifact files for debugging on failure."""
    print(f"\n[live-e2e] === Workspace Dump: {workspace_path} ===")
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
    print("[live-e2e] === End Workspace Dump ===\n")

def _register_compass_dispatch(cfg: dict, workspace_path: str, tl_result_queue: queue.Queue) -> None:
    """Override dispatch_development_task to run Team Lead in a background thread."""
    import asyncio as _asyncio
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry

    registry = get_registry()

    class _InProcessDispatch(BaseTool):
        name = "dispatch_development_task"
        description = "Dispatch a software development task to Team Lead."
        parameters_schema = {
            "type": "object",
            "properties": {
                "task_description": {"type": "string"},
                "jira_key": {"type": "string"},
                "repo_url": {"type": "string"},
                "design_url": {"type": "string"},
            },
            "required": ["task_description"],
        }

        def execute_sync(self, task_description: str = "", jira_key: str = "",
                         repo_url: str = "", design_url: str = "") -> ToolResult:
            effective_jira_key = jira_key or cfg["jira_key"]
            effective_repo_url = repo_url or cfg["scm_repo_url"]
            print(f"[compass-mock] dispatch: jira={effective_jira_key} repo={effective_repo_url}")

            def _run_team_lead():
                loop = _asyncio.new_event_loop()
                _asyncio.set_event_loop(loop)
                try:
                    from framework.task_store import InMemoryTaskStore
                    from agents.team_lead.agent import TeamLeadAgent, team_lead_definition

                    tl_task_store = InMemoryTaskStore()
                    tl_services = _make_services(task_store=tl_task_store)
                    tl_agent = TeamLeadAgent(definition=team_lead_definition, services=tl_services)
                    loop.run_until_complete(tl_agent.start())

                    # Register live boundary tools for Team Lead
                    _register_live_boundary_tools(cfg)

                    msg = {
                        "message": {
                            "messageId": "inline-tl",
                            "role": "ROLE_USER",
                            "parts": [{"text": task_description}],
                            "metadata": {
                                "jiraKey": effective_jira_key,
                                "repoUrl": effective_repo_url,
                                "workspacePath": workspace_path,
                            },
                        }
                    }
                    result = loop.run_until_complete(tl_agent.handle_message(msg))
                    task_id = result["task"]["id"]
                    print(f"[compass-mock] Team Lead task: {task_id}")

                    deadline = time.monotonic() + 960
                    while time.monotonic() < deadline:
                        td = tl_task_store.get_task_dict(task_id)
                        state = td["task"]["status"]["state"]
                        if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"):
                            print(f"[compass-mock] Team Lead reached {state}")
                            tl_result_queue.put(td)
                            return
                        time.sleep(2.0)

                    print("[compass-mock] Team Lead polling timed out")
                    tl_result_queue.put(tl_task_store.get_task_dict(task_id))
                except Exception as exc:
                    import traceback
                    print(f"[compass-mock] Team Lead error: {exc}")
                    traceback.print_exc()
                    tl_result_queue.put({"error": str(exc)})
                finally:
                    loop.close()

            t = threading.Thread(target=_run_team_lead, daemon=True, name="tl-e2e")
            t.start()
            return ToolResult(output=json.dumps({
                "status": "submitted",
                "message": f"Development task dispatched (jira={effective_jira_key}).",
            }))

    registry.register(_InProcessDispatch())
