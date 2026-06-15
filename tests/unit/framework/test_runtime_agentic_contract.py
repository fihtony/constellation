"""Tests for the unified runtime run()/run_agentic() contract."""

from __future__ import annotations

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
    assert copilot.constellation_tools is False
    assert copilot.mcp_servers is False
    assert copilot.allowed_tools is False
    assert copilot.cwd is True

    codex = CodexCLIAdapter().agentic_capabilities()
    assert codex.backend == "codex-cli"
    assert codex.agentic is True
    assert codex.constellation_tools is False
    assert codex.mcp_servers is False
    assert codex.allowed_tools is False
    assert codex.cwd is True


def test_copilot_run_agentic_fails_closed_when_constellation_tools_requested(monkeypatch) -> None:
    from framework.runtime.copilot_cli import CopilotCLIAdapter
    import framework.runtime.copilot_cli as copilot_cli

    def fake_run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("copilot subprocess should not start")

    monkeypatch.setattr(copilot_cli, "_find_copilot_cli", lambda: "copilot")
    monkeypatch.setattr(copilot_cli.subprocess, "run", fake_run)

    result = CopilotCLIAdapter().run_agentic(
        "Do the task.",
        tools=["read_file"],
    )

    assert result.success is False
    assert result.backend_used == "copilot-cli"
    assert "does not support Constellation tools" in result.summary


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
