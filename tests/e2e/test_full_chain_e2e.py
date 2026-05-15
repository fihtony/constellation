"""End-to-end full-chain multi-agent tests.

Gap 8: Real cross-agent A2A lifecycle test.

Tests the full chain:
    Compass → Team Lead → Web Dev → Code Review

TC-11 verifies that the Team Lead's declarative graph workflow correctly
chains multiple tool calls (dispatch_web_dev → dispatch_code_review) end-to-end
and produces a final completion report, using lightweight stub tools that
return hardcoded successful results (no threading or HTTP needed).

TC-12 verifies the full 4-agent chain: Compass calls Team Lead in a fresh
thread (to avoid running a second asyncio event loop on the pytest main thread)
which in turn dispatches to WebDev and CodeReview via stub tools.

TC-13 verifies that PluginManager hooks (before_node / after_node) fire for
every node in the Team Lead graph workflow.

Run:
    pytest tests/e2e/test_full_chain_e2e.py -v -m "not live"
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import time

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_services(runtime=None, task_store=None):
    """Build a fully in-memory AgentServices instance."""
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
        task_store=task_store or InMemoryTaskStore(),
    )


def _poll_task(task_store, task_id: str, timeout: float = 15.0) -> dict:
    """Block until a task reaches a terminal state or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task_dict = task_store.get_task_dict(task_id)
        state = task_dict["task"]["status"]["state"]
        if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
            return task_dict
        time.sleep(0.05)
    return task_store.get_task_dict(task_id)


def _run_in_new_thread(fn, *args, timeout: float = 30.0):
    """Execute a synchronous callable in a brand-new daemon thread.

    Python 3.12 enforces at most one running asyncio event loop per thread.
    Using a fresh thread avoids collisions with the pytest-asyncio main loop.
    """
    result_holder: list = []
    exc_holder: list = []

    def _body():
        try:
            result_holder.append(fn(*args))
        except Exception as exc:  # noqa: BLE001
            exc_holder.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_body)
        future.result(timeout=timeout)

    if exc_holder:
        raise exc_holder[0]
    return result_holder[0] if result_holder else None


# ---------------------------------------------------------------------------
# Shared mock runtime for Team Lead nodes
# ---------------------------------------------------------------------------

class _TeamLeadMockRuntime:
    """Canned responses for Team Lead's LLM-using nodes (analyze + plan)."""

    def run(self, prompt: str, **kwargs) -> dict:
        # analyze_requirements uses ANALYSIS_TEMPLATE which starts with "Analyze"
        if "analyze" in prompt.lower() or "task_type" in prompt.lower():
            raw = json.dumps({
                "task_type": "frontend",
                "complexity": "medium",
                "skills": ["react-nextjs"],
                "summary": "Add login feature for PROJ-123",
            })
        else:
            raw = json.dumps({
                "steps": [
                    {"step": 1, "action": "Clone repo"},
                    {"step": 2, "action": "Implement login page"},
                    {"step": 3, "action": "Run tests"},
                    {"step": 4, "action": "Create PR"},
                ]
            })
        return {"raw_response": raw, "summary": raw}

    def run_agentic(self, task, **kwargs):
        from framework.runtime.adapter import AgenticResult
        return AgenticResult(
            success=True,
            summary="Task completed.",
            turns_used=1,
            backend_used="mock",
        )


# ---------------------------------------------------------------------------
# Shared stub dispatch tools (no real agent calls, no threading)
# ---------------------------------------------------------------------------

def _make_stub_web_dev_tool():
    """Return a stub dispatch_web_dev tool that always succeeds instantly."""
    from framework.tools.base import BaseTool, ToolResult

    class StubDispatchWebDev(BaseTool):
        name = "dispatch_web_dev"
        description = "Stub: immediately returns a successful web-dev result."
        parameters_schema = {"type": "object", "properties": {}, "required": []}

        def execute_sync(
            self,
            task_description: str = "",
            jira_context: dict | None = None,
            design_context=None,
            repo_url: str = "",
            repo_path: str = "",
            workspace_path: str = "",
            context_manifest_path: str = "",
            jira_files: list | None = None,
            design_files: list | None = None,
            revision_feedback: str = "",
        ) -> ToolResult:
            return ToolResult(output=json.dumps({
                "status": "completed",
                "summary": f"Implemented changes for: {task_description[:60]}",
                "prUrl": "https://github.com/test/repo/pull/42",
                "branch": "feature/login-PROJ-123",
            }))

    return StubDispatchWebDev()


def _make_stub_code_review_tool():
    """Return a stub dispatch_code_review tool that always approves instantly."""
    from framework.tools.base import BaseTool, ToolResult

    class StubDispatchCodeReview(BaseTool):
        name = "dispatch_code_review"
        description = "Stub: immediately approves the PR."
        parameters_schema = {"type": "object", "properties": {}, "required": []}

        def execute_sync(
            self,
            pr_url: str = "",
            diff_summary: str = "",
            requirements: str = "",
            jira_context: dict | None = None,
            design_context: dict | None = None,
            workspace_path: str = "",
            context_manifest_path: str = "",
        ) -> ToolResult:
            return ToolResult(output=json.dumps({
                "verdict": "approved",
                "comments": [],
                "summary": "All checks passed. Code is clean.",
            }))

    return StubDispatchCodeReview()


def _make_stub_jira_tool():
    """Return a stub fetch_jira_ticket tool for fail-fast Team Lead tests."""
    from framework.tools.base import BaseTool, ToolResult

    class StubFetchJiraTicket(BaseTool):
        name = "fetch_jira_ticket"
        description = "Stub: returns a minimal Jira ticket payload."
        parameters_schema = {"type": "object", "properties": {}, "required": []}

        def execute_sync(self, ticket_key: str = "") -> ToolResult:
            return ToolResult(output=json.dumps({
                "key": ticket_key or "PROJ-123",
                "summary": "Stub Jira ticket",
                "description": "Stubbed ticket for full-chain tests.",
                "acceptanceCriteria": ["Create the requested change"],
            }))

    return StubFetchJiraTicket()


# ---------------------------------------------------------------------------
# TC-11: Team Lead → (stub) Web Dev → (stub) Code Review
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_team_lead_to_web_dev_to_code_review_full_chain():
    """TC-11: Team Lead workflow chains dispatch_web_dev → dispatch_code_review.

    Stub tools return hardcoded successful results so the workflow always
    reaches report_success and produces a non-empty final report.
    No threading or HTTP calls needed.
    """
    from framework.tools.registry import get_registry
    from framework.task_store import InMemoryTaskStore
    from agents.team_lead.agent import TeamLeadAgent, team_lead_definition

    team_lead_ts = InMemoryTaskStore()
    team_lead_agent = TeamLeadAgent(
        team_lead_definition,
        _make_services(runtime=_TeamLeadMockRuntime(), task_store=team_lead_ts),
    )
    await team_lead_agent.start()

    registry = get_registry()
    original_jira = registry.get("fetch_jira_ticket")
    original_web_dev = registry.get("dispatch_web_dev")
    original_code_review = registry.get("dispatch_code_review")
    registry.register(_make_stub_jira_tool())
    registry.register(_make_stub_web_dev_tool())
    registry.register(_make_stub_code_review_tool())

    try:
        response = await team_lead_agent.handle_message({
            "parts": [{"text": "Implement the login feature for PROJ-123"}],
            "metadata": {
                "jiraKey": "PROJ-123",
                "orchestratorTaskId": "compass-task-001",
            },
        })

        assert "task" in response, f"handle_message returned: {response}"
        task_id = response["task"]["id"]

        final = _poll_task(team_lead_ts, task_id, timeout=20)
        state = final["task"]["status"]["state"]
        assert state == "TASK_STATE_COMPLETED", (
            f"Team Lead task ended in: {state}. "
            f"Status: {final['task']['status']}"
        )

        artifacts = final["task"].get("artifacts", [])
        assert len(artifacts) > 0, "Expected at least one artifact from Team Lead"

        report = artifacts[0]["parts"][0]["text"]
        assert len(report) > 0, (
            f"Team Lead report should not be empty. "
            f"Full artifact: {artifacts[0]}"
        )

        # The report should mention success, PR, or the analysis result
        report_lower = report.lower()
        assert any(kw in report_lower for kw in ("completed", "approved", "pr", "task")), (
            f"Report doesn't contain expected keywords: {report!r}"
        )

        print(f"\n[e2e-chain] Team Lead report:\n{report[:400]}")

    finally:
        if original_jira:
            registry.register(original_jira)
        else:
            registry.unregister("fetch_jira_ticket")
        if original_web_dev:
            registry.register(original_web_dev)
        else:
            registry.unregister("dispatch_web_dev")
        if original_code_review:
            registry.register(original_code_review)
        else:
            registry.unregister("dispatch_code_review")


# ---------------------------------------------------------------------------
# TC-12: Compass → Team Lead → (stub) Web Dev → (stub) Code Review
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_chain_compass_to_code_review():
    """TC-12: Full 4-agent chain: Compass → TeamLead → WebDev → CodeReview.

    Compass mock runtime calls TeamLeadAgent.handle_message() in a fresh
    thread (via _run_in_new_thread) so it avoids the Python 3.12 restriction
    of at most one asyncio event loop per thread.
    """
    from framework.tools.registry import get_registry
    from framework.task_store import InMemoryTaskStore
    from framework.runtime.adapter import AgenticResult
    from agents.compass.agent import CompassAgent, compass_definition
    from agents.team_lead.agent import TeamLeadAgent, team_lead_definition

    compass_ts = InMemoryTaskStore()
    team_lead_ts = InMemoryTaskStore()

    team_lead_agent = TeamLeadAgent(
        team_lead_definition,
        _make_services(runtime=_TeamLeadMockRuntime(), task_store=team_lead_ts),
    )
    await team_lead_agent.start()

    _tl_summary: list[str] = []

    class CompassMockRuntime:
        """Mock runtime that calls TeamLead in a dedicated thread."""

        def run_agentic(self, task: str, **kwargs) -> AgenticResult:
            def _call_team_lead():
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(
                        team_lead_agent.handle_message({
                            "parts": [{"text": task}],
                            "metadata": {
                                "jiraKey": "PROJ-123",
                                "orchestratorTaskId": "compass-e2e-001",
                            },
                        })
                    )
                finally:
                    loop.close()

            tl_response = _run_in_new_thread(_call_team_lead, timeout=5)
            tl_task_id = tl_response["task"]["id"]
            tl_final = _poll_task(team_lead_ts, tl_task_id, timeout=20)
            tl_state = tl_final["task"]["status"]["state"]

            if tl_state == "TASK_STATE_COMPLETED":
                arts = tl_final["task"].get("artifacts", [])
                summary = arts[0]["parts"][0]["text"] if arts else "Completed."
                _tl_summary.append(summary)
                return AgenticResult(
                    success=True,
                    summary=f"Team Lead completed.\n{summary}",
                    turns_used=1,
                    backend_used="mock-compass",
                )
            return AgenticResult(
                success=False,
                summary=f"Team Lead ended in: {tl_state}",
                turns_used=1,
                backend_used="mock-compass",
            )

    compass_agent = CompassAgent(
        compass_definition,
        _make_services(runtime=CompassMockRuntime(), task_store=compass_ts),
    )
    await compass_agent.start()

    registry = get_registry()
    original_jira = registry.get("fetch_jira_ticket")
    original_web_dev = registry.get("dispatch_web_dev")
    original_code_review = registry.get("dispatch_code_review")
    registry.register(_make_stub_jira_tool())
    registry.register(_make_stub_web_dev_tool())
    registry.register(_make_stub_code_review_tool())

    try:
        response = await compass_agent.handle_message({
            "parts": [{"text": "Implement the login feature for PROJ-123"}],
            "metadata": {},
        })

        assert "task" in response
        compass_task_id = response["task"]["id"]

        compass_final = _poll_task(compass_ts, compass_task_id, timeout=35)
        compass_state = compass_final["task"]["status"]["state"]
        assert compass_state == "TASK_STATE_COMPLETED", (
            f"Compass task ended in: {compass_state}"
        )

        compass_arts = compass_final["task"].get("artifacts", [])
        assert len(compass_arts) > 0
        compass_summary = compass_arts[0]["parts"][0]["text"]
        assert len(compass_summary) > 0

        assert len(_tl_summary) >= 1, "Team Lead should have completed"

        print(f"\n[e2e-full] Compass: {compass_summary[:200]}")
        print(f"[e2e-full] TL report: {_tl_summary[0][:200]}")

    finally:
        if original_jira:
            registry.register(original_jira)
        else:
            registry.unregister("fetch_jira_ticket")
        if original_web_dev:
            registry.register(original_web_dev)
        else:
            registry.unregister("dispatch_web_dev")
        if original_code_review:
            registry.register(original_code_review)
        else:
            registry.unregister("dispatch_code_review")


# ---------------------------------------------------------------------------
# TC-13: Plugin hooks fire across the full Team Lead workflow graph
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plugin_events_fired_across_team_lead_workflow():
    """TC-13: PluginManager's before_node / after_node events fire for every
    graph node in the Team Lead workflow when plugin_manager is in RunConfig.
    """
    from framework.plugin import BasePlugin, PluginManager
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore
    from framework.tools.registry import get_registry
    from agents.team_lead.agent import TeamLeadAgent, team_lead_definition

    visited_nodes: list[str] = []

    class NodeRecorderPlugin(BasePlugin):
        async def before_node(self, node_name: str, state: dict):
            visited_nodes.append(f"before:{node_name}")
            return None

        async def after_node(self, node_name: str, state: dict):
            visited_nodes.append(f"after:{node_name}")
            return None

    pm = PluginManager()
    pm.register(NodeRecorderPlugin())

    ts = InMemoryTaskStore()
    services = AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=pm,
        checkpoint_service=InMemoryCheckpointer(),
        runtime=_TeamLeadMockRuntime(),
        registry_client=None,
        task_store=ts,
    )

    agent = TeamLeadAgent(team_lead_definition, services)
    await agent.start()

    registry = get_registry()
    orig_wd = registry.get("dispatch_web_dev")
    orig_cr = registry.get("dispatch_code_review")
    registry.register(_make_stub_web_dev_tool())
    registry.register(_make_stub_code_review_tool())

    try:
        response = await agent.handle_message({
            "parts": [{"text": "Add search feature"}],
            "metadata": {},
        })
        task_id = response["task"]["id"]
        final = _poll_task(ts, task_id, timeout=20)

        assert final["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

        before_events = [e for e in visited_nodes if e.startswith("before:")]
        after_events = [e for e in visited_nodes if e.startswith("after:")]

        assert len(before_events) >= 5, (
            f"Expected >=5 before_node events, got {len(before_events)}: {before_events}"
        )
        assert len(after_events) >= 5, (
            f"Expected >=5 after_node events, got {len(after_events)}: {after_events}"
        )

        print(f"\n[e2e-plugin] Node events: {visited_nodes}")
    finally:
        if orig_wd:
            registry.register(orig_wd)
        else:
            registry.unregister("dispatch_web_dev")
        if orig_cr:
            registry.register(orig_cr)
        else:
            registry.unregister("dispatch_code_review")

