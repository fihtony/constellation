"""Tests for the unified runtime run()/run_agentic() contract."""

from __future__ import annotations

import json
from types import SimpleNamespace


def test_agentic_capabilities_are_explicit_per_backend() -> None:
    from framework.runtime.claude_code import ClaudeCodeAdapter
    from framework.runtime.codex_cli import CodexCLIAdapter
    from framework.runtime.connect_agent.adapter import ConnectAgentAdapter
    from framework.runtime.copilot_cli import CopilotCLIAdapter

    claude = ClaudeCodeAdapter().agentic_capabilities()
    assert claude.backend == "claude-code"
    assert claude.agentic is True
    assert claude.constellation_tools is True
    assert claude.mcp_servers is True
    assert claude.allowed_tools is True
    assert claude.cwd is True

    connect = ConnectAgentAdapter().agentic_capabilities()
    assert connect.backend == "connect-agent"
    assert connect.agentic is True
    assert connect.constellation_tools is True
    assert connect.mcp_servers is False
    assert connect.allowed_tools is True
    assert connect.cwd is False

    copilot = CopilotCLIAdapter().agentic_capabilities()
    assert copilot.backend == "copilot-cli"
    assert copilot.agentic is True
    assert copilot.constellation_tools is True
    assert copilot.mcp_servers is False
    assert copilot.allowed_tools is True
    assert copilot.cwd is True

    codex = CodexCLIAdapter().agentic_capabilities()
    assert codex.backend == "codex-cli"
    assert codex.agentic is True
    assert codex.constellation_tools is True
    assert codex.mcp_servers is False
    assert codex.allowed_tools is True
    assert codex.cwd is True


def test_copilot_run_agentic_uses_managed_tool_loop_when_constellation_tools_requested(monkeypatch) -> None:
    from framework.runtime.copilot_cli import CopilotCLIAdapter
    import framework.runtime.copilot_cli as copilot_cli

    responses = [
        {"raw_response": '{"action": "tool", "tool": "echo", "arguments": {"text": "hi"}}'},
        {"raw_response": '{"action": "final", "summary": "echoed"}'},
    ]

    def fake_run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("copilot subprocess should not start")

    def fake_single_shot(self, prompt, **kwargs):
        return responses.pop(0)

    class FakeRegistry:
        def list_schemas(self, tool_names):
            return [{"function": {"name": name, "parameters": {"type": "object"}}} for name in tool_names]

        def execute_sync(self, name, arguments):
            assert name == "echo"
            return '{"echo": "hi"}'

    monkeypatch.setattr(copilot_cli, "_find_copilot_cli", lambda: "copilot")
    monkeypatch.setattr(copilot_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(CopilotCLIAdapter, "run", fake_single_shot)
    monkeypatch.setattr("framework.tools.registry.get_registry", lambda: FakeRegistry())

    result = CopilotCLIAdapter().run_agentic(
        "Do the task.",
        tools=["echo"],
        allowed_tools=["echo"],
    )

    assert result.success is True
    assert result.backend_used == "copilot-cli"
    assert result.summary == "echoed"
    assert result.tool_calls == [{"tool": "echo", "arguments": {"text": "hi"}, "turn": 1}]


def test_codex_run_agentic_uses_managed_tool_loop_when_constellation_tools_requested(monkeypatch) -> None:
    from framework.runtime.codex_cli import CodexCLIAdapter
    import framework.runtime.codex_cli as codex_cli

    responses = [
        {"raw_response": '{"action": "tool", "tool": "echo", "arguments": {"text": "hi"}}'},
        {"raw_response": '{"action": "final", "summary": "echoed"}'},
    ]

    def fake_run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("codex subprocess should not start")

    def fake_single_shot(self, prompt, **kwargs):
        return responses.pop(0)

    class FakeRegistry:
        def list_schemas(self, tool_names):
            return [{"function": {"name": name, "parameters": {"type": "object"}}} for name in tool_names]

        def execute_sync(self, name, arguments):
            assert name == "echo"
            return '{"echo": "hi"}'

    monkeypatch.setattr(codex_cli, "_find_codex_cli", lambda: "codex")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(CodexCLIAdapter, "run", fake_single_shot)
    monkeypatch.setattr("framework.tools.registry.get_registry", lambda: FakeRegistry())

    result = CodexCLIAdapter().run_agentic(
        "Do the task.",
        tools=["echo"],
        allowed_tools=["echo"],
    )

    assert result.success is True
    assert result.backend_used == "codex-cli"
    assert result.summary == "echoed"
    assert result.tool_calls == [{"tool": "echo", "arguments": {"text": "hi"}, "turn": 1}]


def test_managed_agentic_loop_repairs_plain_text_response_before_success(monkeypatch) -> None:
    from framework.runtime.copilot_cli import CopilotCLIAdapter

    responses = [
        {
            "raw_response": (
                "<think>I should inspect the repository first.</think>\n\n"
                "I will start by listing files."
            )
        },
        {
            "raw_response": (
                '{"action": "tool", "tool": "write_file", '
                '"arguments": {"path": "app.py", "content": "print(1)"}}'
            )
        },
        {"raw_response": '{"action": "final", "summary": "created app.py"}'},
    ]
    prompts: list[str] = []

    def fake_single_shot(self, prompt, **kwargs):
        prompts.append(prompt)
        return responses.pop(0)

    class FakeRegistry:
        def list_schemas(self, tool_names):
            return [{"function": {"name": name, "parameters": {"type": "object"}}} for name in tool_names]

        def execute_sync(self, name, arguments):
            assert name == "write_file"
            return '{"written": true, "path": "app.py"}'

    monkeypatch.setattr(CopilotCLIAdapter, "run", fake_single_shot)
    monkeypatch.setattr("framework.tools.registry.get_registry", lambda: FakeRegistry())

    result = CopilotCLIAdapter().run_agentic(
        "Create the project files.",
        tools=["write_file"],
        allowed_tools=["write_file"],
        max_turns=3,
    )

    assert result.success is True
    assert result.summary == "created app.py"
    assert result.tool_calls == [
        {
            "tool": "write_file",
            "arguments": {"path": "app.py", "content": "print(1)"},
            "turn": 2,
        }
    ]
    assert "Invalid response format" in prompts[1]


def test_managed_agentic_loop_fails_when_backend_never_returns_protocol_json(monkeypatch) -> None:
    from framework.runtime.codex_cli import CodexCLIAdapter

    def fake_single_shot(self, prompt, **kwargs):
        return {"raw_response": "I am done without using the required JSON protocol."}

    class FakeRegistry:
        def list_schemas(self, tool_names):
            return [{"function": {"name": name, "parameters": {"type": "object"}}} for name in tool_names]

    monkeypatch.setattr(CodexCLIAdapter, "run", fake_single_shot)
    monkeypatch.setattr("framework.tools.registry.get_registry", lambda: FakeRegistry())

    result = CodexCLIAdapter().run_agentic(
        "Create the project files.",
        tools=["write_file"],
        allowed_tools=["write_file"],
        max_turns=2,
    )

    assert result.success is False
    assert result.backend_used == "codex-cli"
    assert result.turns_used == 2
    assert "did not return valid managed-loop JSON" in result.summary
    assert result.tool_calls == []


def test_codex_run_agentic_fails_closed_when_mcp_servers_requested(monkeypatch) -> None:
    from framework.runtime.codex_cli import CodexCLIAdapter
    import framework.runtime.codex_cli as codex_cli

    def fake_run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("codex subprocess should not start")

    monkeypatch.setattr(codex_cli, "_find_codex_cli", lambda: "codex")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)

    result = CodexCLIAdapter().run_agentic(
        "Do the task.",
        mcp_servers={"repo": {"command": "server"}},
    )

    assert result.success is False
    assert result.backend_used == "codex-cli"
    assert "does not support MCP servers" in result.summary


def test_codex_run_agentic_spools_large_prompt_to_file(monkeypatch, tmp_path) -> None:
    from framework.runtime.codex_cli import CodexCLIAdapter
    import framework.runtime.codex_cli as codex_cli

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        prompt_arg = cmd[cmd.index("-q") + 1]
        assert "large body" not in prompt_arg
        marker = "Full task prompt file:"
        prompt_path = prompt_arg.split(marker, 1)[1].splitlines()[0].strip()
        captured["prompt_file_text"] = open(prompt_path, encoding="utf-8").read()
        return SimpleNamespace(returncode=0, stdout="completed", stderr="")

    monkeypatch.setenv("CONSTELLATION_CLI_ARG_PROMPT_LIMIT", "80")
    monkeypatch.setattr(codex_cli, "_find_codex_cli", lambda: "codex")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)

    result = CodexCLIAdapter().run_agentic("large body " * 100, cwd=str(tmp_path))

    assert result.success is True
    assert "large body" in captured["prompt_file_text"]


def test_connect_run_agentic_filters_tools_by_allowed_tools(monkeypatch) -> None:
    from framework.runtime.connect_agent.adapter import ConnectAgentAdapter
    import framework.runtime.connect_agent.adapter as connect_adapter

    captured = {}

    def fake_chat_completion(messages, **kwargs):
        captured["tools"] = kwargs.get("tools")
        return {
            "choices": [
                {
                    "message": {"content": "done"},
                    "finish_reason": "stop",
                }
            ]
        }

    class FakeRegistry:
        def list_schemas(self, tool_names):
            return [{"function": {"name": name}} for name in tool_names]

    monkeypatch.setattr(connect_adapter, "call_chat_completion", fake_chat_completion)
    monkeypatch.setattr("framework.tools.registry.get_registry", lambda: FakeRegistry())

    result = ConnectAgentAdapter().run_agentic(
        "Use tools if needed.",
        tools=["read_file", "write_file"],
        allowed_tools=["read_file"],
    )

    assert result.success is True
    assert captured["tools"] == [{"function": {"name": "read_file"}}]


def test_connect_run_agentic_does_not_expose_all_tools_when_tool_list_empty(monkeypatch) -> None:
    from framework.runtime.connect_agent.adapter import ConnectAgentAdapter
    import framework.runtime.connect_agent.adapter as connect_adapter

    captured = {}

    def fake_chat_completion(messages, **kwargs):
        captured["tools"] = kwargs.get("tools")
        return {
            "choices": [
                {
                    "message": {"content": "done"},
                    "finish_reason": "stop",
                }
            ]
        }

    class FakeRegistry:
        def list_schemas(self, tool_names=None):
            if tool_names is None:
                return [{"function": {"name": "dangerous"}}]
            return [{"function": {"name": name}} for name in tool_names]

    monkeypatch.setattr(connect_adapter, "call_chat_completion", fake_chat_completion)
    monkeypatch.setattr("framework.tools.registry.get_registry", lambda: FakeRegistry())

    result = ConnectAgentAdapter().run_agentic(
        "Do not use tools.",
        tools=[],
        allowed_tools=[],
    )

    assert result.success is True
    assert captured["tools"] is None


def test_claude_run_agentic_maps_allowed_tools_to_effective_tool_surface(monkeypatch) -> None:
    from framework.runtime.claude_code import ClaudeCodeAdapter
    import framework.runtime.claude_code as claude_code

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="done", stderr="")

    monkeypatch.setattr(claude_code, "_find_claude_cli", lambda: "claude")
    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)

    result = ClaudeCodeAdapter().run_agentic(
        "Do the task.",
        tools=["read_file", "write_file"],
        allowed_tools=["mcp__constellation-tools__read_file"],
    )

    assert result.success is True
    assert "--allowedTools" in captured["cmd"]
    assert "mcp__constellation-tools__read_file" in captured["cmd"]


def test_agentic_policy_maps_constellation_tools_for_claude_code() -> None:
    from framework.agentic_policy import build_agentic_execution_policy
    from framework.runtime.adapter import AgenticCapabilities

    runtime = SimpleNamespace(
        agentic_capabilities=lambda: AgenticCapabilities(
            backend="claude-code",
            agentic=True,
            constellation_tools=True,
            mcp_servers=True,
            cwd=True,
            allowed_tools=True,
        )
    )

    policy = build_agentic_execution_policy(runtime, ["read_file", "run_command"])

    assert policy.backend == "claude-code"
    assert policy.tools == ["read_file", "run_command"]
    assert policy.allowed_tools == [
        "mcp__constellation_tools__read_file",
        "mcp__constellation_tools__run_command",
    ]
    assert policy.enforced is True


def test_agentic_policy_keeps_tools_for_fail_closed_unsupported_backends() -> None:
    from framework.agentic_policy import build_agentic_execution_policy
    from framework.runtime.adapter import AgenticCapabilities

    runtime = SimpleNamespace(
        agentic_capabilities=lambda: AgenticCapabilities(
            backend="copilot-cli",
            agentic=True,
            constellation_tools=False,
            cwd=True,
            allowed_tools=False,
        )
    )

    policy = build_agentic_execution_policy(runtime, ["read_file"])

    assert policy.backend == "copilot-cli"
    assert policy.tools == ["read_file"]
    assert policy.allowed_tools == []
    assert policy.enforced is False
    assert "does not support Constellation tools" in policy.fail_closed_reason


def test_agentic_step_validation_rejects_tool_calls_outside_policy() -> None:
    from framework.agentic_policy import AgenticExecutionPolicy, validate_agentic_step_result
    from framework.runtime.adapter import AgenticResult

    policy = AgenticExecutionPolicy(
        backend="connect-agent",
        tools=["read_file"],
        allowed_tools=["read_file"],
        enforced=True,
    )
    result = AgenticResult(
        success=True,
        summary="done",
        backend_used="connect-agent",
        tool_calls=[{"tool": "run_command", "turn": 1}],
    )

    validation = validate_agentic_step_result(policy, result)

    assert validation.passed is False
    assert validation.gate_name == "agentic_step_policy"
    assert "run_command" in validation.feedback


def test_agentic_step_gate_writes_structured_audit_record(tmp_path) -> None:
    from framework.agentic_policy import (
        AgenticExecutionPolicy,
        record_agentic_step_gate,
        validate_agentic_step_result,
    )
    from framework.runtime.adapter import AgenticResult

    policy = AgenticExecutionPolicy(
        backend="connect-agent",
        tools=["read_file", "run_command"],
        allowed_tools=["read_file", "run_command"],
        enforced=True,
    )
    result = AgenticResult(
        success=True,
        summary="done",
        backend_used="connect-agent",
        tool_calls=[{"tool": "run_command", "turn": 1}],
        turns_used=1,
    )
    validation = validate_agentic_step_result(policy, result)

    audit_path = record_agentic_step_gate(
        workspace_path=str(tmp_path),
        agent_id="web-dev",
        task_id="task-gate",
        step="implement_changes",
        policy=policy,
        result=result,
        validation=validation,
    )

    records = [json.loads(line) for line in open(audit_path, encoding="utf-8")]
    assert records[-1]["agent_id"] == "web-dev"
    assert records[-1]["task_id"] == "task-gate"
    assert records[-1]["step"] == "implement_changes"
    assert records[-1]["backend"] == "connect-agent"
    assert records[-1]["validation"]["passed"] is True
    assert records[-1]["tool_calls"][0]["tool"] == "run_command"
