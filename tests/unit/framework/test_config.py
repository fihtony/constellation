"""Unit tests for framework/config.py — unified layered configuration loader."""
from __future__ import annotations

import os
import tempfile
import textwrap

import pytest


@pytest.fixture()
def project_dir(tmp_path):
    """Create a minimal project directory with global and agent configs."""
    # Global config
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "constellation.yaml").write_text(textwrap.dedent("""\
        project:
          name: constellation
          version: "2.0.0"
        runtime:
          backend: connect-agent
          model: gpt-5-mini
        skills:
          directory: skills
        data:
          directory: data
        container:
          runtime: docker
          network: constellation-network
        registry:
          url: http://registry:9000
    """))

    # Agent config — team_lead
    agent_dir = tmp_path / "agents" / "team_lead"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(textwrap.dedent("""\
        agent_id: team-lead
        name: "Team Lead Agent"
        description: "Intelligence layer"
        mode: task
        execution_mode: persistent
        runtime_backend: connect-agent
        model: gpt-5-mini
        tools:
          - dispatch_agent
          - query_registry
        port: 8030
    """))

    # Agent config — web_dev (with skills)
    wd_dir = tmp_path / "agents" / "web_dev"
    wd_dir.mkdir(parents=True)
    (wd_dir / "config.yaml").write_text(textwrap.dedent("""\
        agent_id: web-dev
        name: "Web Dev Agent"
        mode: task
        execution_mode: per-task
        model: gpt-5-mini
        default_skills:
          - react-nextjs
          - testing
        tools:
          - read_file
          - write_file
        permissions:
          scm: read-write
    """))

    # Permissions
    perm_dir = config_dir / "permissions"
    perm_dir.mkdir()
    (perm_dir / "development.yaml").write_text(textwrap.dedent("""\
        allowed_tools:
          - read_file
          - write_file
        denied_tools: []
        scm: read-write
        filesystem: workspace-only
    """))

    return tmp_path


class TestLoadGlobalConfig:
    def test_loads_global_defaults(self, project_dir):
        from framework.config import load_global_config

        cfg = load_global_config(project_dir)
        assert cfg.get("project.name") == "constellation"
        assert cfg.get("runtime.backend") == "connect-agent"
        assert cfg.get("runtime.model") == "gpt-5-mini"
        assert cfg.get("container.runtime") == "docker"

    def test_missing_file_returns_empty(self, tmp_path):
        from framework.config import load_global_config

        cfg = load_global_config(tmp_path)
        assert cfg.get("runtime.backend") is None


class TestLoadAgentConfig:
    def test_merges_global_and_agent(self, project_dir):
        from framework.config import load_agent_config

        cfg = load_agent_config("team-lead", project_dir)
        # Agent-specific values
        assert cfg.get("agent_id") == "team-lead"
        assert cfg.get("name") == "Team Lead Agent"
        assert cfg.get("port") == 8030
        # Global values inherited
        assert cfg.get("project.name") == "constellation"
        assert cfg.get("container.network") == "constellation-network"

    def test_agent_overrides_global(self, project_dir):
        """Agent-specific model should override global default."""
        from framework.config import load_agent_config

        cfg = load_agent_config("team-lead", project_dir)
        assert cfg.get("model") == "gpt-5-mini"

    def test_env_override(self, project_dir, monkeypatch):
        from framework.config import load_agent_config

        monkeypatch.setenv("CONSTELLATION_RUNTIME_MODEL", "gpt-6")
        cfg = load_agent_config("team-lead", project_dir)
        assert cfg.get("runtime.model") == "gpt-6"

    def test_constellation_prefix_beats_openai_model(self, project_dir, monkeypatch):
        """CONSTELLATION_RUNTIME_MODEL should win over OPENAI_MODEL."""
        from framework.config import load_agent_config

        monkeypatch.setenv("OPENAI_MODEL", "gpt-low-priority")
        monkeypatch.setenv("CONSTELLATION_RUNTIME_MODEL", "gpt-high-priority")
        cfg = load_agent_config("team-lead", project_dir)
        assert cfg.get("runtime.model") == "gpt-high-priority"

    def test_cli_override(self, project_dir):
        from framework.config import load_agent_config

        cfg = load_agent_config(
            "team-lead", project_dir, overrides={"port": 9999}
        )
        assert cfg.get("port") == 9999

    def test_nonexistent_agent_returns_global(self, project_dir):
        from framework.config import load_agent_config

        cfg = load_agent_config("nonexistent", project_dir)
        assert cfg.get("project.name") == "constellation"
        assert cfg.get("agent_id") is None


class TestBuildAgentDefinitionFromConfig:
    def test_builds_definition_dict(self, project_dir):
        from framework.config import build_agent_definition_from_config

        defn = build_agent_definition_from_config("team-lead", project_dir)
        assert defn["agent_id"] == "team-lead"
        assert defn["name"] == "Team Lead Agent"
        assert defn["mode"] == "task"
        assert "dispatch_agent" in defn["tools"]

    def test_skills_from_default_skills(self, project_dir):
        from framework.config import build_agent_definition_from_config

        defn = build_agent_definition_from_config("web-dev", project_dir)
        assert defn["skills"] == ["react-nextjs", "testing"]


class TestDeepMerge:
    def test_deep_merge_dicts(self):
        from framework.config import _deep_merge

        base = {"a": {"x": 1, "y": 2}, "b": 10}
        override = {"a": {"y": 99, "z": 3}, "c": 20}
        result = _deep_merge(base, override)
        assert result == {"a": {"x": 1, "y": 99, "z": 3}, "b": 10, "c": 20}

    def test_lists_are_replaced(self):
        from framework.config import _deep_merge

        base = {"tools": ["a", "b"]}
        override = {"tools": ["c"]}
        result = _deep_merge(base, override)
        assert result["tools"] == ["c"]

    def test_scalar_override(self):
        from framework.config import _deep_merge

        base = {"model": "old"}
        result = _deep_merge(base, {"model": "new"})
        assert result["model"] == "new"


class TestConstellationConfig:
    def test_get_nested_path(self, project_dir):
        from framework.config import load_global_config

        cfg = load_global_config(project_dir)
        assert cfg.get("registry.url") == "http://registry:9000"

    def test_get_default(self, project_dir):
        from framework.config import load_global_config

        cfg = load_global_config(project_dir)
        assert cfg.get("nonexistent.key", "default") == "default"

    def test_contains(self, project_dir):
        from framework.config import load_global_config

        cfg = load_global_config(project_dir)
        assert "project" in cfg
        assert "nonexistent" not in cfg

    def test_to_dict_returns_copy(self, project_dir):
        from framework.config import load_global_config

        cfg = load_global_config(project_dir)
        d = cfg.to_dict()
        d["new_key"] = "value"
        # Original config data is not affected
        assert "new_key" not in cfg.data
