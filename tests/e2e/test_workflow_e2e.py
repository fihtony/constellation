"""End-to-end workflow tests for the Constellation v2 framework.

These tests exercise the full agent workflow stack:
  Compass workflow → task classification → (mock) dispatch → summarize

Tests that require a live LLM are decorated with @pytest.mark.live and
use the connect-agent runtime configured via tests/.env.

Non-LLM tests run against deterministic in-memory service implementations
and always pass without external dependencies.

Run:
    pytest tests/e2e/ -v                     # all tests (LLM skipped if unavailable)
    pytest tests/e2e/ -v -m live             # LLM-required tests only
    pytest tests/e2e/ -v -m "not live"       # deterministic tests only
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest


# =============================================================================
# Helpers
# =============================================================================

def _make_services(runtime=None):
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry

    return AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=runtime,
        registry_client=None,
    )


# =============================================================================
# TC-01: ToolRegistry – register, execute_sync, list_schemas
# =============================================================================

def test_tool_registry_register_and_execute():
    """ToolRegistry can register tools and execute them synchronously."""
    from framework.tools.registry import ToolRegistry
    from framework.tools.base import BaseTool, ToolResult

    class AddTool(BaseTool):
        name = "add"
        description = "Add two numbers."
        parameters_schema = {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        }

        def execute_sync(self, a: float = 0, b: float = 0) -> ToolResult:
            return ToolResult(output=json.dumps({"result": a + b}))

    registry = ToolRegistry()
    registry.register(AddTool())

    result_str = registry.execute_sync("add", '{"a": 3, "b": 4}')
    result = json.loads(result_str)
    assert result.get("result") == 7.0

    schemas = registry.list_schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "add"


def test_tool_registry_missing_tool():
    """execute_sync returns error JSON for unknown tool names."""
    from framework.tools.registry import ToolRegistry

    registry = ToolRegistry()
    out = registry.execute_sync("nonexistent", "{}")
    data = json.loads(out)
    assert "error" in data
    assert "nonexistent" in data["error"]


# =============================================================================
# TC-02: multi-runtime factory
# =============================================================================

def test_runtime_factory_creates_correct_backends():
    """get_runtime() returns an instance of the correct adapter class."""
    from framework.runtime.adapter import get_runtime
    from framework.runtime.connect_agent.adapter import ConnectAgentAdapter
    from framework.runtime.copilot_cli import CopilotCLIAdapter
    from framework.runtime.claude_code import ClaudeCodeAdapter
    from framework.runtime.codex_cli import CodexCLIAdapter

    assert isinstance(get_runtime("connect-agent"), ConnectAgentAdapter)
    assert isinstance(get_runtime("copilot-cli"), CopilotCLIAdapter)
    assert isinstance(get_runtime("claude-code"), ClaudeCodeAdapter)
    assert isinstance(get_runtime("codex-cli"), CodexCLIAdapter)

    # Aliases
    assert isinstance(get_runtime("claude"), ClaudeCodeAdapter)
    assert isinstance(get_runtime("copilot"), CopilotCLIAdapter)
    assert isinstance(get_runtime("codex"), CodexCLIAdapter)


def test_runtime_factory_unknown_raises():
    from framework.runtime.adapter import get_runtime
    import framework.runtime.adapter as _adapter
    # Clear cached instance to avoid interference
    _adapter._INSTANCES.pop("bad-backend", None)
    with pytest.raises(KeyError):
        get_runtime("bad-backend")


# =============================================================================
# TC-03: JiraAgentAdapter with mock client (no network)
# =============================================================================

@pytest.mark.asyncio
async def test_jira_adapter_direct_mode_mock():
    """JiraAgentAdapter dispatches correctly with a mock JiraClient."""
    from agents.jira.adapter import JiraAgentAdapter, jira_definition
    from framework.tools.base import ToolResult

    class MockJiraClient:
        def fetch_ticket(self, key):
            return {"key": key, "fields": {"summary": "Mock ticket"}}, "ok"

        def get_myself(self):
            return {"displayName": "Test User"}, "ok"

    services = _make_services()
    adapter = JiraAgentAdapter(
        definition=jira_definition,
        services=services,
        dispatch_mode="direct",
        jira_client=MockJiraClient(),
    )

    msg = {
        "parts": [{"text": "PROJ-001"}],
        "metadata": {"requestedCapability": "jira.ticket.fetch", "ticketKey": "PROJ-001"},
    }
    response = await adapter.handle_message(msg)
    task = response["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    result = json.loads(task["artifacts"][0]["parts"][0]["text"])
    assert result["status"] == "ok"
    assert result["ticket"]["key"] == "PROJ-001"


# =============================================================================
# TC-04: SCMAgentAdapter with mock client (no network)
# =============================================================================

@pytest.mark.asyncio
async def test_scm_adapter_direct_mode_mock():
    """SCMAgentAdapter dispatches correctly with a mock BitbucketClient."""
    from agents.scm.adapter import SCMAgentAdapter, scm_definition

    class MockSCMClient:
        def get_repo(self, project, repo, **kwargs):
            return {"slug": repo, "project": {"key": project}}, "ok"

        def list_branches(self, project, repo, **kwargs):
            return [{"displayId": "main", "isDefault": True}], "ok"

    services = _make_services()
    adapter = SCMAgentAdapter(
        definition=scm_definition,
        services=services,
        dispatch_mode="direct",
        scm_client=MockSCMClient(),
    )

    msg = {
        "parts": [{"text": "PROJ/my-repo"}],
        "metadata": {
            "requestedCapability": "scm.repo.inspect",
            "project": "PROJ",
            "repo": "my-repo",
        },
    }
    response = await adapter.handle_message(msg)
    task = response["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    result = json.loads(task["artifacts"][0]["parts"][0]["text"])
    assert result["status"] == "ok"
    assert result["repo"]["slug"] == "my-repo"


# =============================================================================
# TC-05: UIDesignAgentAdapter with mock client (no network)
# =============================================================================

@pytest.mark.asyncio
async def test_ui_design_adapter_direct_mode_mock():
    """UIDesignAgentAdapter dispatches correctly with a mock FigmaClient."""
    from agents.ui_design.adapter import UIDesignAgentAdapter, ui_design_definition

    class MockFigmaClient:
        def get_file(self, url_or_key, **kwargs):
            return {
                "name": "Mock Design",
                "lastModified": "2025-01-01",
                "document": {"children": [{"id": "1:1", "name": "Page 1"}]},
            }, "ok"

    services = _make_services()
    adapter = UIDesignAgentAdapter(
        definition=ui_design_definition,
        services=services,
        dispatch_mode="direct",
        figma_client=MockFigmaClient(),
    )

    msg = {
        "parts": [{"text": "https://www.figma.com/design/abc123/Test"}],
        "metadata": {
            "requestedCapability": "figma.page.fetch",
            "figmaUrl": "https://www.figma.com/design/abc123/Test",
        },
    }
    response = await adapter.handle_message(msg)
    task = response["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    result = json.loads(task["artifacts"][0]["parts"][0]["text"])
    assert result["status"] == "ok"
    assert result["name"] == "Mock Design"
    assert len(result["pages"]) == 1


# =============================================================================
# TC-06: ReAct agentic loop with mock LLM (tool calling)
# =============================================================================

def test_react_agentic_loop_with_mock_llm():
    """ConnectAgentAdapter.run_agentic() executes the ReAct tool-calling loop."""
    import json
    import time
    from framework.runtime.connect_agent.adapter import ConnectAgentAdapter
    from framework.tools.registry import ToolRegistry
    from framework.tools.base import BaseTool, ToolResult
    import framework.tools.registry as _reg
    import framework.runtime.connect_agent.transport as _transport

    # ---- Register a simple tool -----------------------------------------
    class UpperCaseTool(BaseTool):
        name = "to_upper"
        description = "Convert text to uppercase."
        parameters_schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

        def execute_sync(self, text: str = "") -> ToolResult:
            return ToolResult(output=json.dumps({"result": text.upper()}))

    test_registry = ToolRegistry()
    test_registry.register(UpperCaseTool())

    original_registry = _reg._default_registry
    _reg._default_registry = test_registry

    # ---- Patch call_chat_completion to simulate LLM ----------------------
    call_count = 0

    def _mock_llm(messages, *, model, timeout=120, max_tokens=4096, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if tools and call_count == 1:
            # First call: LLM wants to use the tool
            return {
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "tc-001",
                            "type": "function",
                            "function": {
                                "name": "to_upper",
                                "arguments": '{"text": "hello world"}',
                            },
                        }],
                    },
                }]
            }
        # Second call: LLM has the tool result, finishes
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "The result is HELLO WORLD",
                },
            }]
        }

    original_call = _transport.call_chat_completion
    _transport.call_chat_completion = _mock_llm

    try:
        adapter = ConnectAgentAdapter()
        result = adapter.run_agentic(
            task="Convert 'hello world' to uppercase using the to_upper tool.",
            tools=["to_upper"],
            max_turns=5,
            timeout=30,
        )
    finally:
        _transport.call_chat_completion = original_call
        _reg._default_registry = original_registry

    assert result.success, f"ReAct loop failed: {result.summary}"
    assert "HELLO WORLD" in result.summary.upper() or len(result.tool_calls) >= 1
    assert result.turns_used == 2
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["tool"] == "to_upper"
    print(f"[e2e] ReAct loop: {result.summary!r}, turns={result.turns_used}")


# =============================================================================
# TC-07: Compass classify_task node (no LLM, heuristic fallback)
# =============================================================================

@pytest.mark.asyncio
async def test_compass_classify_task_heuristic():
    """classify_task node classifies development/office tasks via heuristic."""
    from agents.compass.nodes import classify_task

    dev_state = {"user_request": "Implement jira ticket PROJ-123 and create a PR"}
    result = await classify_task(dev_state)
    assert result.get("task_classification") == "development"

    office_state = {"user_request": "Summarize this PDF document"}
    result2 = await classify_task(office_state)
    assert result2.get("task_classification") == "office"

    general_state = {"user_request": "Hello, how are you?"}
    result3 = await classify_task(general_state)
    assert result3.get("task_classification") == "general"


# =============================================================================
# TC-08: Full compass workflow (in-memory, no LLM)
# =============================================================================

@pytest.mark.asyncio
async def test_compass_workflow_full_in_memory():
    """Compass workflow executes all nodes in-memory without LLM."""
    from agents.compass.agent import CompassAgent
    from agents.compass.agent import compass_definition
    from framework.workflow import WorkflowRunner

    services = _make_services()
    agent = CompassAgent(compass_definition, services)
    await agent.start()

    initial_state = {
        "user_request": "What is the capital of France?",
        "session_id": "test-e2e-001",
    }
    from framework.workflow import RunConfig
    config = RunConfig(session_id="test-e2e-001", thread_id="thread-001")
    runner = WorkflowRunner(agent._compiled_workflow, config)
    final_state = await runner.run(initial_state)

    assert final_state is not None
    # Compass should produce a user-facing reply
    assert "final_response" in final_state or "task_classification" in final_state
    print(f"[e2e] Compass final state keys: {list(final_state.keys())}")


# =============================================================================
# TC-09: Live E2E with real LLM — classify and respond
# =============================================================================

@pytest.mark.live
@pytest.mark.asyncio
async def test_compass_classify_with_real_llm(llm_available, llm_base_url, llm_model):
    """classify_task node uses real LLM when runtime is available."""
    if not llm_available:
        pytest.skip(f"LLM not reachable at {llm_base_url}")

    os.environ.setdefault("OPENAI_BASE_URL", llm_base_url)
    os.environ.setdefault("OPENAI_MODEL", llm_model)

    from framework.runtime.adapter import get_runtime
    from agents.compass.nodes import classify_task

    runtime = get_runtime("connect-agent", model=llm_model)

    state = {
        "_runtime": runtime,
        "user_request": "Implement the feature from PROJ-456 and open a pull request",
    }
    result = await classify_task(state)
    classification = result.get("task_classification", "")
    assert classification in ("development", "office", "general"), (
        f"Unexpected classification: {classification!r}"
    )
    print(f"[e2e-live] LLM classified as: {classification!r}")
