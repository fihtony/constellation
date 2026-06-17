"""Tests for task-scoped agent logging."""

from __future__ import annotations


def test_agent_logger_allows_message_metadata(monkeypatch, tmp_path) -> None:
    from framework.devlog import AgentLogger

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

    logger = AgentLogger("task-log", "web-dev")
    logger.info("progress update", message="backend turn 1", level="metadata")

    log_path = tmp_path / "task-log" / "web-dev" / "agent.log"
    text = log_path.read_text(encoding="utf-8")
    assert "progress update" in text
    assert "message='backend turn 1'" in text
    assert "level='metadata'" in text


def test_workspace_logger_allows_message_metadata(monkeypatch, tmp_path) -> None:
    from framework.devlog import WorkspaceLogger

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

    workspace = tmp_path / "task-workspace"
    logger = WorkspaceLogger(str(workspace), "team-lead")
    logger.warn("dispatch warning", message="child returned error")

    text = (tmp_path / "task-workspace" / "team-lead" / "agent.log").read_text(encoding="utf-8")
    assert "dispatch warning" in text
    assert "message='child returned error'" in text
