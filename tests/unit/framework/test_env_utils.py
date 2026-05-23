"""Tests for shared runtime env loading helpers."""

from __future__ import annotations

from pathlib import Path

from framework.env_utils import load_agent_environment, resolve_runtime_env_file


def test_resolve_runtime_env_file_prefers_override(tmp_path, monkeypatch):
    override = tmp_path / "secrets" / ".runtime.env"
    override.parent.mkdir(parents=True)
    override.write_text("AGENT_RUNTIME=claude-code\n", encoding="utf-8")
    monkeypatch.setenv("CONSTELLATION_RUNTIME_ENV_FILE", str(override))

    resolved = resolve_runtime_env_file(str(tmp_path))

    assert resolved == str(override)


def test_load_agent_environment_skips_runtime_env_when_not_requested(tmp_path, monkeypatch):
    project_root = Path(tmp_path)
    (project_root / "config").mkdir()
    (project_root / "config" / ".env").write_text("ANTHROPIC_AUTH_TOKEN=shared-token\n", encoding="utf-8")
    agent_dir = project_root / "agents" / "jira"
    agent_dir.mkdir(parents=True)
    (agent_dir / ".env").write_text("JIRA_TOKEN=jira-token\n", encoding="utf-8")

    monkeypatch.delenv("CONSTELLATION_RUNTIME_ENV_FILE", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)

    load_agent_environment(str(project_root), "jira", include_runtime_env=False)

    assert "ANTHROPIC_AUTH_TOKEN" not in monkeypatch._setitem
    assert "ANTHROPIC_AUTH_TOKEN" not in __import__("os").environ
    assert __import__("os").environ["JIRA_TOKEN"] == "jira-token"


def test_load_agent_environment_loads_runtime_env_for_runtime_enabled_agent(tmp_path, monkeypatch):
    project_root = Path(tmp_path)
    (project_root / "config").mkdir()
    (project_root / "config" / ".runtime.env").write_text(
        "AGENT_RUNTIME=claude-code\nANTHROPIC_AUTH_TOKEN=shared-token\n",
        encoding="utf-8",
    )
    agent_dir = project_root / "agents" / "office"
    agent_dir.mkdir(parents=True)
    (agent_dir / ".env").write_text("OFFICE_BACKUP_ENABLED=true\n", encoding="utf-8")

    monkeypatch.delenv("CONSTELLATION_RUNTIME_ENV_FILE", raising=False)
    monkeypatch.delenv("AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OFFICE_BACKUP_ENABLED", raising=False)

    load_agent_environment(str(project_root), "office", include_runtime_env=True)

    import os

    assert os.environ["AGENT_RUNTIME"] == "claude-code"
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "shared-token"
    assert os.environ["OFFICE_BACKUP_ENABLED"] == "true"