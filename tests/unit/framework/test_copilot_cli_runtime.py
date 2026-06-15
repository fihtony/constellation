"""Tests for the Copilot CLI runtime adapter."""

from __future__ import annotations

import json
from types import SimpleNamespace


def test_run_uses_copilot_provider_endpoint_for_single_shot(monkeypatch) -> None:
    from framework.runtime.copilot_cli import CopilotCLIAdapter

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {"content": "provider response"},
                    "finish_reason": "stop",
                }]
            }).encode("utf-8")

    def _urlopen(request, timeout=120):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        return _Response()

    monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://api.minimaxi.com/v1")
    monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("CONNECT_AGENT_URL", "http://connect-agent.test:1288")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://openai-compatible.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setattr("framework.runtime.connect_agent.transport.urlopen", _urlopen)

    result = CopilotCLIAdapter().run("Summarize this.")

    assert result["summary"] == "provider response"
    assert result["raw_response"] == "provider response"
    assert result["backend_used"] == "copilot-cli"
    assert captured["url"] == "https://api.minimaxi.com/v1/chat/completions"
    assert captured["authorization"] == "Bearer provider-key"


def test_run_agentic_accepts_plugin_manager(monkeypatch) -> None:
    from framework.runtime.copilot_cli import CopilotCLIAdapter
    import framework.runtime.copilot_cli as copilot_cli

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="completed", stderr="")

    monkeypatch.setattr(copilot_cli, "_find_copilot_cli", lambda: "copilot")
    monkeypatch.setattr(copilot_cli.subprocess, "run", fake_run)

    result = CopilotCLIAdapter().run_agentic(
        "Do the task.",
        plugin_manager=object(),
        timeout=5,
    )

    assert result.success is True
    assert result.summary == "completed"
    assert captured["cmd"] == [
        "copilot",
        "--prompt",
        "Do the task.",
        "--silent",
        "--no-color",
        "--no-auto-update",
        "--output-format",
        "text",
        "--allow-all-tools",
        "--allow-all-paths",
        "--no-ask-user",
        "--secret-env-vars=COPILOT_PROVIDER_API_KEY,COPILOT_GITHUB_TOKEN,GH_TOKEN,GITHUB_TOKEN",
    ]
    assert captured["kwargs"]["timeout"] == 5


def test_run_agentic_passes_model_from_agent_model(monkeypatch) -> None:
    from framework.runtime.copilot_cli import CopilotCLIAdapter
    import framework.runtime.copilot_cli as copilot_cli

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="completed", stderr="")

    monkeypatch.setenv("AGENT_MODEL", "MiniMax-M2.7")
    monkeypatch.delenv("COPILOT_MODEL", raising=False)
    monkeypatch.setattr(copilot_cli, "_find_copilot_cli", lambda: "copilot")
    monkeypatch.setattr(copilot_cli.subprocess, "run", fake_run)

    result = CopilotCLIAdapter().run_agentic("Do the task.")

    assert result.success is True
    assert captured["env"]["COPILOT_MODEL"] == "MiniMax-M2.7"
    assert captured["cmd"][-2:] == ["--model", "MiniMax-M2.7"]
