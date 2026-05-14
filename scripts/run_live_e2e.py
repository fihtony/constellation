#!/usr/bin/env python3
"""Live E2E integration test: Compass → Team Lead → (Jira + Dev Agent) workflow.

This test exercises the full multi-agent chain with real LLM + real Jira.
Boundary agents (Jira, Design, SCM) run in-process (direct adapter mode).
The LLM (via Copilot Connect at localhost:1288) drives all reasoning.

Usage:
    # Ensure Copilot Connect is running on localhost:1288
    source .venv/bin/activate
    python scripts/run_live_e2e.py

    # Or with explicit env:
    OPENAI_BASE_URL=http://localhost:1288/v1 python scripts/run_live_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Load test env
# ---------------------------------------------------------------------------

def _load_test_env() -> dict[str, str]:
    env_file = Path(__file__).parent.parent / "tests" / ".env"
    env: dict[str, str] = {}
    if not env_file.exists():
        print(f"[WARN] tests/.env not found at {env_file}")
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


# ---------------------------------------------------------------------------
# Setup env
# ---------------------------------------------------------------------------

# Set LLM env
os.environ.setdefault("OPENAI_BASE_URL", _env("OPENAI_BASE_URL", "http://localhost:1288/v1"))
os.environ.setdefault("OPENAI_MODEL", _env("OPENAI_MODEL", "gpt-5.4-mini"))
os.environ.setdefault("OPENAI_API_KEY", _env("OPENAI_API_KEY", ""))
os.environ.setdefault("AGENT_RUNTIME", "connect-agent")


# ---------------------------------------------------------------------------
# Verify LLM connectivity
# ---------------------------------------------------------------------------

def _check_llm() -> bool:
    """Verify the LLM endpoint is reachable."""
    import urllib.request
    import urllib.error
    base = os.environ["OPENAI_BASE_URL"].rstrip("/")
    url = f"{base}/models"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"[OK] LLM endpoint reachable: {url} → {resp.status}")
            return True
    except Exception as exc:
        print(f"[ERROR] LLM endpoint unreachable: {url} → {exc}")
        return False


# ---------------------------------------------------------------------------
# Build services
# ---------------------------------------------------------------------------

def _make_services(runtime):
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore

    return AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=runtime,
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )


# ---------------------------------------------------------------------------
# Setup Jira (direct in-process adapter)
# ---------------------------------------------------------------------------

def _setup_jira_tool():
    """Register a mock Jira tool that calls the real Jira client directly."""
    from agents.jira.client import JiraClient
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry

    token = _env("TEST_JIRA_TOKEN")
    email = _env("TEST_JIRA_EMAIL")
    ticket_url = _env("TEST_JIRA_TICKET_URL")

    if not all([token, email, ticket_url]):
        print("[WARN] Jira credentials missing — Jira tool will return mock data")
        return

    jira_client = JiraClient.from_ticket_url(
        ticket_url=ticket_url,
        token=token,
        email=email,
    )

    class DirectJiraFetch(BaseTool):
        name = "fetch_jira_ticket"
        description = "Fetch a Jira ticket (direct in-process call)."
        parameters_schema = {
            "type": "object",
            "properties": {
                "ticket_key": {"type": "string", "description": "Jira ticket key."},
            },
            "required": ["ticket_key"],
        }

        def execute_sync(self, ticket_key: str = "") -> ToolResult:
            print(f"  [jira-tool] Fetching ticket: {ticket_key}")
            try:
                issue, status = jira_client.fetch_ticket(ticket_key)
                if not issue:
                    return ToolResult(output=json.dumps({"error": f"Ticket not found: {status}"}))
                fields = issue.get("fields", {})
                # Description may be ADF (dict) or plain string
                raw_desc = fields.get("description") or ""
                if isinstance(raw_desc, dict):
                    # Extract text from ADF (simple extraction)
                    desc_text = json.dumps(raw_desc, ensure_ascii=False)[:2000]
                else:
                    desc_text = str(raw_desc)[:2000]
                result = {
                    "key": issue.get("key", ticket_key),
                    "summary": fields.get("summary", ""),
                    "description": desc_text,
                    "status": fields.get("status", {}).get("name", ""),
                    "issueType": fields.get("issuetype", {}).get("name", ""),
                    "priority": fields.get("priority", {}).get("name", ""),
                    "labels": fields.get("labels", []),
                    "assignee": (fields.get("assignee") or {}).get("displayName", ""),
                }
                print(f"  [jira-tool] Got: {result['key']} - {result['summary'][:60]}")
                return ToolResult(output=json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                print(f"  [jira-tool] Error: {exc}")
                return ToolResult(output=json.dumps({"error": str(exc)}))

    registry = get_registry()
    registry.unregister("fetch_jira_ticket")
    registry.register(DirectJiraFetch())
    print("[OK] Jira tool registered (direct in-process)")


# ---------------------------------------------------------------------------
# Setup mock dev/review tools (simplified for this E2E)
# ---------------------------------------------------------------------------

def _setup_dev_tools():
    """Register simplified dev and code review tools that simulate success."""
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry

    class MockWebDev(BaseTool):
        name = "dispatch_web_dev"
        description = "Dispatch a web dev task (mock for E2E)."
        parameters_schema = {
            "type": "object",
            "properties": {
                "task_description": {"type": "string"},
                "jira_context": {"type": "object"},
                "design_context": {"type": "object"},
                "repo_url": {"type": "string"},
                "revision_feedback": {"type": "string"},
            },
            "required": ["task_description"],
        }

        def execute_sync(self, **kwargs) -> ToolResult:
            desc = kwargs.get("task_description", "")
            print(f"  [web-dev-mock] Received task: {desc[:80]}...")
            # Simulate dev agent work
            result = {
                "status": "completed",
                "summary": f"Implemented changes for the task. All modifications applied to the codebase.",
                "prUrl": "https://bitbucket.example.com/projects/PROJ/repos/sample-app/pull-requests/1",
                "branch": "feature/PROJ-123-implementation",
            }
            print(f"  [web-dev-mock] Returning: PR={result['prUrl']}")
            return ToolResult(output=json.dumps(result))

    class MockCodeReview(BaseTool):
        name = "dispatch_code_review"
        description = "Code review (mock for E2E)."
        parameters_schema = {
            "type": "object",
            "properties": {
                "pr_url": {"type": "string"},
                "diff_summary": {"type": "string"},
                "requirements": {"type": "string"},
            },
            "required": [],
        }

        def execute_sync(self, **kwargs) -> ToolResult:
            pr_url = kwargs.get("pr_url", "")
            print(f"  [code-review-mock] Reviewing PR: {pr_url}")
            result = {
                "verdict": "approved",
                "comments": [],
                "summary": "Code review passed. Implementation is correct and follows best practices.",
            }
            print(f"  [code-review-mock] Verdict: {result['verdict']}")
            return ToolResult(output=json.dumps(result))

    class MockDesign(BaseTool):
        name = "fetch_design"
        description = "Fetch design context (mock for E2E)."
        parameters_schema = {
            "type": "object",
            "properties": {
                "figma_url": {"type": "string"},
                "stitch_project_id": {"type": "string"},
                "screen_name": {"type": "string"},
            },
            "required": [],
        }

        def execute_sync(self, **kwargs) -> ToolResult:
            print(f"  [design-mock] Fetching design...")
            return ToolResult(output=json.dumps({"status": "no_design_url_provided"}))

    class MockClarification(BaseTool):
        name = "request_clarification"
        description = "Ask user for clarification."
        parameters_schema = {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
            },
            "required": ["question"],
        }

        def execute_sync(self, **kwargs) -> ToolResult:
            q = kwargs.get("question", "")
            print(f"  [clarification-mock] Question: {q}")
            return ToolResult(output=json.dumps({
                "status": "input_required",
                "question": q,
            }))

    registry = get_registry()
    for tool_cls in [MockWebDev, MockCodeReview, MockDesign, MockClarification]:
        tool = tool_cls()
        registry.unregister(tool.name)
        registry.register(tool)

    print("[OK] Dev/Review/Design tools registered (mock)")


# ---------------------------------------------------------------------------
# Run Team Lead workflow directly (in-process, with real LLM)
# ---------------------------------------------------------------------------

async def run_team_lead_workflow(
    jira_ticket_url: str,
    runtime,
) -> dict:
    """Run Team Lead's graph workflow end-to-end with real LLM + real Jira."""
    from agents.team_lead.agent import TeamLeadAgent, team_lead_definition
    from framework.a2a.protocol import TaskState

    # Parse Jira key from URL
    from agents.jira.client import JiraClient
    jira_key = JiraClient.parse_ticket_key(jira_ticket_url)
    print(f"\n{'='*70}")
    print(f"  TEAM LEAD WORKFLOW — ticket: {jira_key}")
    print(f"{'='*70}\n")

    services = _make_services(runtime)
    agent = TeamLeadAgent(team_lead_definition, services)
    await agent.start()

    # Build the message
    message = {
        "parts": [{"text": f"Implement {jira_ticket_url}"}],
        "metadata": {
            "jiraKey": jira_key,
            "repoUrl": _env("TEST_GITHUB_REPO_URL", ""),
        },
    }

    # Send message — this starts the background workflow
    print("[step] Sending message to Team Lead...")
    response = await agent.handle_message(message)
    task_id = response.get("task", response).get("id", "")
    print(f"[step] Task created: {task_id}")
    print(f"[step] Initial state: {response.get('task', response).get('status', {}).get('state', '')}")

    # Poll until done
    deadline = time.time() + 120  # 2 min timeout
    last_state = ""
    while time.time() < deadline:
        task_dict = await agent.get_task(task_id)
        task_info = task_dict.get("task", task_dict)
        state = task_info.get("status", {}).get("state", "")
        if state != last_state:
            print(f"[step] State transition: {last_state or 'INITIAL'} → {state}")
            last_state = state
        if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"):
            break
        await asyncio.sleep(1)

    # Final result
    final = await agent.get_task(task_id)
    final_task = final.get("task", final)
    final_state = final_task.get("status", {}).get("state", "")
    print(f"\n{'='*70}")
    print(f"  FINAL STATE: {final_state}")

    if final_state == "TASK_STATE_COMPLETED":
        artifacts = final_task.get("artifacts", [])
        for art in artifacts:
            for part in art.get("parts", []):
                if "text" in part:
                    print(f"\n  RESULT:\n{part['text']}")
            meta = art.get("metadata", {})
            if meta.get("prUrl"):
                print(f"  PR URL: {meta['prUrl']}")
            if meta.get("branch"):
                print(f"  Branch: {meta['branch']}")
    elif final_state == "TASK_STATE_FAILED":
        msg = final_task.get("status", {}).get("message", "")
        print(f"  ERROR: {msg}")
    elif final_state == "TASK_STATE_INPUT_REQUIRED":
        msg = final_task.get("status", {}).get("message", "")
        print(f"  NEEDS INPUT: {msg}")

    print(f"{'='*70}\n")
    return final_task


# ---------------------------------------------------------------------------
# Run Compass → Team Lead (full chain, in-process)
# ---------------------------------------------------------------------------

async def run_compass_full_chain(
    user_request: str,
    runtime,
) -> dict:
    """Run the full Compass → Team Lead chain.

    Since both agents share the same process, Compass's
    dispatch_development_task tool is patched to call Team Lead directly
    instead of over HTTP.
    """
    from agents.compass.agent import CompassAgent, compass_definition
    from agents.compass.tools import register_compass_tools
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry
    from framework.a2a.protocol import TaskState

    print(f"\n{'='*70}")
    print(f"  COMPASS AGENT — Full Chain Test")
    print(f"  Request: {user_request}")
    print(f"{'='*70}\n")

    # Override the dispatch_development_task tool to run Team Lead in-process
    class InProcessDevDispatch(BaseTool):
        name = "dispatch_development_task"
        description = (
            "Dispatch a software development task to the Team Lead Agent "
            "(in-process for E2E testing)."
        )
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

        def execute_sync(self, **kwargs) -> ToolResult:
            desc = kwargs.get("task_description", "")
            jira_key = kwargs.get("jira_key", "")
            repo_url = kwargs.get("repo_url", "")
            print(f"  [compass→team-lead] Dispatching: {desc[:80]}")
            print(f"  [compass→team-lead] Jira: {jira_key}, Repo: {repo_url}")

            # Run Team Lead synchronously in a new thread with a fresh event loop
            import concurrent.futures
            from agents.team_lead.agent import TeamLeadAgent, team_lead_definition

            tl_services = _make_services(runtime)

            def _run_tl_sync():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    async def _inner():
                        tl_agent = TeamLeadAgent(team_lead_definition, tl_services)
                        await tl_agent.start()
                        msg = {
                            "parts": [{"text": desc}],
                            "metadata": {
                                "jiraKey": jira_key,
                                "repoUrl": repo_url,
                            },
                        }
                        resp = await tl_agent.handle_message(msg)
                        task_id = resp.get("task", resp).get("id", "")
                        # Poll
                        deadline = time.time() + 120
                        while time.time() < deadline:
                            task_dict = await tl_agent.get_task(task_id)
                            state = task_dict.get("task", task_dict).get("status", {}).get("state", "")
                            if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"):
                                return task_dict
                            await asyncio.sleep(1)
                        return await tl_agent.get_task(task_id)
                    return loop.run_until_complete(_inner())
                finally:
                    loop.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_run_tl_sync)
                result = future.result(timeout=180)

            task_data = result.get("task", result)
            artifacts = task_data.get("artifacts", [])
            summary_text = ""
            for art in artifacts:
                for part in art.get("parts", []):
                    if "text" in part:
                        summary_text = part["text"]
                        break
                if summary_text:
                    break

            return ToolResult(output=json.dumps({
                "status": "completed",
                "summary": summary_text or "Team Lead completed the task.",
            }))

    # Register tools
    register_compass_tools()
    registry = get_registry()
    # Replace dispatch_development_task with in-process version
    registry.unregister("dispatch_development_task")
    registry.register(InProcessDevDispatch())

    # Create and run Compass
    services = _make_services(runtime)
    agent = CompassAgent(compass_definition, services)
    await agent.start()

    message = {
        "parts": [{"text": user_request}],
        "metadata": {},
    }

    print("[step] Sending to Compass Agent...")
    response = await agent.handle_message(message)
    task_info = response.get("task", response)
    task_id = task_info.get("id", "")
    state = task_info.get("status", {}).get("state", "")
    print(f"[step] Task: {task_id}, State: {state}")

    # Compass is synchronous (handle_message returns final state)
    artifacts = task_info.get("artifacts", [])
    for art in artifacts:
        for part in art.get("parts", []):
            if "text" in part:
                print(f"\n  COMPASS FINAL RESPONSE:\n  {part['text'][:500]}")

    print(f"\n{'='*70}")
    print(f"  COMPASS FINAL STATE: {state}")
    print(f"{'='*70}\n")
    return task_info


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print("\n" + "=" * 70)
    print("  CONSTELLATION v2 — Live E2E Integration Test")
    print("=" * 70)
    print(f"  LLM: {os.environ.get('OPENAI_BASE_URL')}")
    print(f"  Model: {os.environ.get('OPENAI_MODEL')}")
    print(f"  Jira Ticket: {_env('TEST_JIRA_TICKET_URL')}")
    print(f"  Repo: {_env('TEST_GITHUB_REPO_URL')}")
    print("=" * 70 + "\n")

    # Step 1: Check LLM
    if not _check_llm():
        print("\n[FATAL] Cannot proceed without LLM. Ensure Copilot Connect is running.")
        sys.exit(1)

    # Step 2: Get runtime
    from framework.runtime.adapter import get_runtime
    runtime = get_runtime()
    print(f"[OK] Runtime: connect-agent")

    # Step 3: Quick LLM smoke test
    print("\n[step] LLM smoke test...")
    result = runtime.run(
        prompt="Return ONLY the JSON: {\"status\": \"ok\"}",
        system_prompt="You are a test helper. Return exactly what is asked.",
        max_tokens=50,
    )
    raw = result.get("raw_response", "")
    print(f"  LLM response: {raw[:100]}")
    if "ok" not in raw.lower() and "status" not in raw.lower():
        print("[WARN] LLM smoke test response unexpected, but continuing...")

    # Step 4: Setup tools
    print("\n[step] Setting up tools...")
    _setup_jira_tool()
    _setup_dev_tools()

    # Step 5: Run Team Lead workflow (core test)
    jira_url = _env("TEST_JIRA_TICKET_URL")
    if not jira_url:
        print("[FATAL] TEST_JIRA_TICKET_URL not set")
        sys.exit(1)

    print("\n" + "=" * 70)
    print("  TEST 1: Team Lead Workflow (direct)")
    print("=" * 70)
    tl_result = await run_team_lead_workflow(jira_url, runtime)
    tl_state = tl_result.get("status", {}).get("state", "")

    # Step 6: Run full Compass → Team Lead chain
    print("\n" + "=" * 70)
    print("  TEST 2: Compass → Team Lead (full chain)")
    print("=" * 70)
    compass_result = await run_compass_full_chain(
        f"implement {jira_url}",
        runtime,
    )
    compass_state = compass_result.get("status", {}).get("state", "")

    # Summary
    print("\n" + "=" * 70)
    print("  FINAL SUMMARY")
    print("=" * 70)
    print(f"  Test 1 (Team Lead direct): {tl_state}")
    print(f"  Test 2 (Compass chain):    {compass_state}")

    success = (
        tl_state == "TASK_STATE_COMPLETED"
        and compass_state == "TASK_STATE_COMPLETED"
    )
    print(f"\n  Overall: {'PASS ✓' if success else 'FAIL ✗'}")
    print("=" * 70 + "\n")

    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
