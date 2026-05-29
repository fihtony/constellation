"""Unit tests for framework/config.py — unified layered configuration loader."""
from __future__ import annotations

import textwrap

import pytest


@pytest.fixture()
def project_dir(tmp_path):
    """Create a minimal project directory with global and agent configs."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "constellation.yaml").write_text(textwrap.dedent("""\
        project:
          name: constellation
          version: "2.0.0"
        runtime:
          backend: claude-code
          model: claude-haiku-4-5-20251001
        boundary:
          jira:
            backend: mcp
          scm:
            backend: github-mcp
          ui_design:
            default_provider: stitch
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

    agent_dir = tmp_path / "agents" / "team_lead"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(textwrap.dedent("""\
        agent_id: team-lead
        name: "Team Lead Agent"
        description: "Intelligence layer"
        mode: task
        execution_mode: persistent
        runtime_backend: claude-code
        model: claude-haiku-4-5-20251001
        permission_profile: team-lead
        port: 8030
    """))

    wd_dir = tmp_path / "agents" / "web_dev"
    wd_dir.mkdir(parents=True)
    (wd_dir / "config.yaml").write_text(textwrap.dedent("""\
        agent_id: web-dev
        name: "Web Dev Agent"
        mode: task
        execution_mode: per-task
        model: gpt-5-mini
        permission_profile: web-dev
        launch_spec:
          image: constellation-v2-web-dev:latest
          port: 8050
          extra_binds:
            - /tmp/source:/app/source:ro
        default_skills:
          - react-nextjs
          - testing
    """))

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
    (perm_dir / "team-lead.yaml").write_text(textwrap.dedent("""\
        allowed_tools:
          - dispatch_agent
          - query_registry
        denied_tools: []
        scm: read-write
        filesystem: workspace-only
    """))
    (perm_dir / "web-dev.yaml").write_text(textwrap.dedent("""\
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
        assert cfg.get("runtime.backend") == "claude-code"
        assert cfg.get("runtime.model") == "claude-haiku-4-5-20251001"
        assert cfg.get("container.runtime") == "docker"

    def test_missing_file_returns_empty(self, tmp_path):
        from framework.config import load_global_config

        cfg = load_global_config(tmp_path)
        assert cfg.get("runtime.backend") is None


class TestLoadAgentConfig:
    def test_merges_global_and_agent(self, project_dir):
        from framework.config import load_agent_config

        cfg = load_agent_config("team-lead", project_dir)
        assert cfg.get("agent_id") == "team-lead"
        assert cfg.get("name") == "Team Lead Agent"
        assert cfg.get("port") == 8030
        assert cfg.get("project.name") == "constellation"
        assert cfg.get("container.network") == "constellation-network"

    def test_agent_overrides_global(self, project_dir):
        """Agent-specific model should override global default."""
        from framework.config import load_agent_config

        cfg = load_agent_config("team-lead", project_dir)
        assert cfg.get("model") == "claude-haiku-4-5-20251001"

    def test_env_override(self, project_dir, monkeypatch):
        from framework.config import load_agent_config

        monkeypatch.setenv("CONSTELLATION_RUNTIME_MODEL", "gpt-6")
        cfg = load_agent_config("team-lead", project_dir)
        assert cfg.get("runtime.model") == "gpt-6"

    def test_claude_runtime_env_override(self, project_dir, monkeypatch):
        from framework.config import load_agent_config

        monkeypatch.setenv("AGENT_RUNTIME", "claude-code")
        monkeypatch.setenv("ANTHROPIC_MODEL", "MiniMax-M2.7")
        cfg = load_agent_config("team-lead", project_dir)
        assert cfg.get("runtime.backend") == "claude-code"
        assert cfg.get("runtime.model") == "MiniMax-M2.7"

    def test_constellation_prefix_beats_openai_model(self, project_dir, monkeypatch):
        """CONSTELLATION_RUNTIME_MODEL should win over OPENAI_MODEL."""
        from framework.config import load_agent_config

        monkeypatch.setenv("OPENAI_MODEL", "gpt-low-priority")
        monkeypatch.setenv("CONSTELLATION_RUNTIME_MODEL", "gpt-high-priority")
        cfg = load_agent_config("team-lead", project_dir)
        assert cfg.get("runtime.model") == "gpt-high-priority"

    def test_cli_override(self, project_dir):
        from framework.config import load_agent_config

        cfg = load_agent_config("team-lead", project_dir, overrides={"port": 9999})
        assert cfg.get("port") == 9999

    def test_nonexistent_agent_returns_global(self, project_dir):
        from framework.config import load_agent_config

        cfg = load_agent_config("nonexistent", project_dir)
        assert cfg.get("project.name") == "constellation"
        assert cfg.get("agent_id") is None


class TestBuildAgentDefinitionFromConfig:
    def test_builds_definition_dict(self, project_dir):
        from framework.config import build_agent_definition_from_config

        definition = build_agent_definition_from_config("team-lead", project_dir)
        assert definition["agent_id"] == "team-lead"
        assert definition["name"] == "Team Lead Agent"
        assert definition["mode"] == "task"
        assert "dispatch_agent" in definition["tools"]

    def test_skills_from_default_skills(self, project_dir):
        from framework.config import build_agent_definition_from_config

        definition = build_agent_definition_from_config("web-dev", project_dir)
        assert definition["skills"] == ["react-nextjs", "testing"]

    def test_launch_spec_is_preserved(self, project_dir):
        from framework.config import build_agent_definition_from_config

        definition = build_agent_definition_from_config("web-dev", project_dir)
        assert definition["launch_spec"] == {
            "image": "constellation-v2-web-dev:latest",
            "port": 8050,
            "extra_binds": ["/tmp/source:/app/source:ro"],
        }

    def test_permissions_default_from_permission_profile(self, project_dir):
        from framework.config import build_agent_definition_from_config

        definition = build_agent_definition_from_config("web-dev", project_dir)
        assert definition["permissions"]["scm"] == "read-write"

    def test_tools_default_from_permission_profile(self, project_dir):
        from framework.config import build_agent_definition_from_config

        definition = build_agent_definition_from_config("web-dev", project_dir)
        assert definition["tools"] == ["read_file", "write_file"]


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
        data = cfg.to_dict()
        data["new_key"] = "value"
        assert "new_key" not in cfg.data


# ---------------------------------------------------------------------------
# Boundary config tests
# ---------------------------------------------------------------------------

class TestBoundaryDefaults:
    """boundary.* section is present in the YAML and readable via config loader."""

    def test_jira_default_backend(self, project_dir):
        from framework.config import load_global_config

        cfg = load_global_config(project_dir)
        assert cfg.get("boundary.jira.backend") == "mcp"

    def test_scm_default_backend(self, project_dir):
        from framework.config import load_global_config

        cfg = load_global_config(project_dir)
        assert cfg.get("boundary.scm.backend") == "github-mcp"

    def test_ui_design_default_provider(self, project_dir):
        from framework.config import load_global_config

        cfg = load_global_config(project_dir)
        assert cfg.get("boundary.ui_design.default_provider") == "stitch"


class TestBoundaryEnvOverrides:
    """Environment variables override boundary defaults from YAML."""

    def test_jira_backend_env_override(self, project_dir, monkeypatch):
        from framework.config import load_global_config

        monkeypatch.setenv("JIRA_BACKEND", "rest")
        cfg = load_global_config(project_dir)
        assert cfg.get("boundary.jira.backend") == "rest"

    def test_scm_backend_env_override(self, project_dir, monkeypatch):
        from framework.config import load_global_config

        monkeypatch.setenv("SCM_BACKEND", "bitbucket")
        cfg = load_global_config(project_dir)
        assert cfg.get("boundary.scm.backend") == "bitbucket"

    def test_ui_design_provider_env_override(self, project_dir, monkeypatch):
        from framework.config import load_global_config

        monkeypatch.setenv("UI_DESIGN_DEFAULT_PROVIDER", "figma")
        cfg = load_global_config(project_dir)
        assert cfg.get("boundary.ui_design.default_provider") == "figma"


class TestGetBoundaryBackend:
    """get_boundary_backend() returns correct values with YAML defaults and env overrides."""

    def test_jira_default(self, project_dir):
        from framework.config import get_boundary_backend

        result = get_boundary_backend("jira", project_dir)
        assert result == "mcp"

    def test_scm_default(self, project_dir):
        from framework.config import get_boundary_backend

        result = get_boundary_backend("scm", project_dir)
        assert result == "github-mcp"

    def test_ui_design_default(self, project_dir):
        from framework.config import get_boundary_backend

        result = get_boundary_backend("ui_design", project_dir)
        assert result == "stitch"

    def test_jira_env_override(self, project_dir, monkeypatch):
        from framework.config import get_boundary_backend

        monkeypatch.setenv("JIRA_BACKEND", "rest")
        result = get_boundary_backend("jira", project_dir)
        assert result == "rest"

    def test_scm_env_override(self, project_dir, monkeypatch):
        from framework.config import get_boundary_backend

        monkeypatch.setenv("SCM_BACKEND", "github-rest")
        result = get_boundary_backend("scm", project_dir)
        assert result == "github-rest"

    def test_unknown_domain_raises(self, project_dir):
        from framework.config import get_boundary_backend

        with pytest.raises(ValueError, match="Unknown boundary domain"):
            get_boundary_backend("unknown_domain", project_dir)

    def test_fallback_when_boundary_section_absent(self, tmp_path):
        """When constellation.yaml has no boundary section, use hardcoded defaults."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "constellation.yaml").write_text(
            "project:\n  name: test\nruntime:\n  backend: claude-code\n"
        )
        from framework.config import get_boundary_backend

        assert get_boundary_backend("jira", tmp_path) == "mcp"
        assert get_boundary_backend("scm", tmp_path) == "github-mcp"
        assert get_boundary_backend("ui_design", tmp_path) == "stitch"


# ---------------------------------------------------------------------------
# validate_startup_config tests
# ---------------------------------------------------------------------------

class TestValidateStartupConfig:
    """validate_startup_config() enforces consistency rules."""

    def test_valid_default_config_passes(self, project_dir, monkeypatch):
        """Default config (claude-code + docker + mcp jira + github-mcp scm + stitch) passes."""
        from framework.config import validate_startup_config

        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
        # No SCM_BASE_URL set — so no URL/backend conflict check
        monkeypatch.delenv("SCM_BASE_URL", raising=False)
        warnings = validate_startup_config(project_dir)
        # stitch without STITCH_API_KEY → warning only
        assert isinstance(warnings, list)

    def test_bitbucket_with_github_url_fails(self, project_dir, monkeypatch):
        """SCM_BACKEND=bitbucket + GitHub URL should raise ConfigValidationError."""
        from framework.config import ConfigValidationError, validate_startup_config

        monkeypatch.setenv("SCM_BACKEND", "bitbucket")
        monkeypatch.setenv("SCM_BASE_URL", "https://github.com/my-org")
        with pytest.raises(ConfigValidationError, match="SCM_BACKEND=bitbucket"):
            validate_startup_config(project_dir, skip_credential_check=True)

    def test_github_rest_with_bitbucket_url_fails(self, project_dir, monkeypatch):
        """SCM_BACKEND=github-rest + Bitbucket URL should raise ConfigValidationError."""
        from framework.config import ConfigValidationError, validate_startup_config

        monkeypatch.setenv("SCM_BACKEND", "github-rest")
        monkeypatch.setenv("SCM_BASE_URL", "https://bitbucket.my-company.com")
        with pytest.raises(ConfigValidationError, match="SCM_BACKEND="):
            validate_startup_config(project_dir, skip_credential_check=True)

    def test_figma_provider_without_token_fails(self, project_dir, monkeypatch):
        """UI_DESIGN_DEFAULT_PROVIDER=figma without FIGMA_TOKEN should fail."""
        from framework.config import ConfigValidationError, validate_startup_config

        monkeypatch.setenv("UI_DESIGN_DEFAULT_PROVIDER", "figma")
        monkeypatch.delenv("FIGMA_TOKEN", raising=False)
        with pytest.raises(ConfigValidationError, match="FIGMA_TOKEN"):
            validate_startup_config(project_dir)

    def test_figma_provider_with_token_passes(self, project_dir, monkeypatch):
        """UI_DESIGN_DEFAULT_PROVIDER=figma with FIGMA_TOKEN set should pass."""
        from framework.config import validate_startup_config

        monkeypatch.setenv("UI_DESIGN_DEFAULT_PROVIDER", "figma")
        monkeypatch.setenv("FIGMA_TOKEN", "figd_test_token")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
        monkeypatch.delenv("SCM_BASE_URL", raising=False)
        warnings = validate_startup_config(project_dir)
        assert isinstance(warnings, list)
        # No error for figma with token
        assert not any("FIGMA_TOKEN" in w for w in warnings)

    def test_stitch_without_api_key_is_warning_not_error(self, project_dir, monkeypatch):
        """stitch provider without STITCH_API_KEY should warn, not fail."""
        from framework.config import validate_startup_config

        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
        monkeypatch.delenv("STITCH_API_KEY", raising=False)
        monkeypatch.delenv("SCM_BASE_URL", raising=False)
        warnings = validate_startup_config(project_dir)
        assert any("STITCH_API_KEY" in w for w in warnings)

    def test_claude_code_without_token_fails(self, project_dir, monkeypatch):
        """AGENT_RUNTIME=claude-code without ANTHROPIC_AUTH_TOKEN should fail."""
        from framework.config import ConfigValidationError, validate_startup_config

        monkeypatch.setenv("AGENT_RUNTIME", "claude-code")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("SCM_BASE_URL", raising=False)
        with pytest.raises(ConfigValidationError, match="ANTHROPIC_AUTH_TOKEN"):
            validate_startup_config(project_dir)

    def test_connect_agent_without_url_is_warning(self, project_dir, monkeypatch):
        """AGENT_RUNTIME=connect-agent without CONNECT_AGENT_URL should warn."""
        from framework.config import validate_startup_config

        monkeypatch.setenv("AGENT_RUNTIME", "connect-agent")
        monkeypatch.delenv("CONNECT_AGENT_URL", raising=False)
        monkeypatch.delenv("SCM_BASE_URL", raising=False)
        warnings = validate_startup_config(project_dir)
        assert any("CONNECT_AGENT_URL" in w for w in warnings)

    def test_copilot_cli_without_token_fails(self, project_dir, monkeypatch):
        """AGENT_RUNTIME=copilot-cli without COPILOT_GITHUB_TOKEN should fail."""
        from framework.config import ConfigValidationError, validate_startup_config

        monkeypatch.setenv("AGENT_RUNTIME", "copilot-cli")
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("SCM_BASE_URL", raising=False)
        with pytest.raises(ConfigValidationError, match="COPILOT_GITHUB_TOKEN"):
            validate_startup_config(project_dir)

    def test_invalid_container_runtime_fails(self, project_dir, monkeypatch):
        """CONTAINER_RUNTIME=unknown should fail."""
        from framework.config import ConfigValidationError, validate_startup_config

        monkeypatch.setenv("CONTAINER_RUNTIME", "kubernetes")
        monkeypatch.setenv("AGENT_RUNTIME", "connect-agent")
        monkeypatch.delenv("SCM_BASE_URL", raising=False)
        with pytest.raises(ConfigValidationError, match="CONTAINER_RUNTIME="):
            validate_startup_config(project_dir)

    def test_skip_credential_check_bypasses_token_checks(self, project_dir, monkeypatch):
        """skip_credential_check=True skips all credential-related checks."""
        from framework.config import validate_startup_config

        monkeypatch.setenv("AGENT_RUNTIME", "claude-code")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("SCM_BASE_URL", raising=False)
        # Should NOT raise even though ANTHROPIC_AUTH_TOKEN is missing
        warnings = validate_startup_config(project_dir, skip_credential_check=True)
        assert isinstance(warnings, list)


class TestSharedSelectorLeakageCheck:
    """_check_agent_env_leakage() warns when shared selectors appear in agent .env."""

    def test_warns_when_shared_key_in_agent_env(self, tmp_path):
        """If JIRA_BACKEND is in agents/jira/.env, a warning is produced."""
        from framework.config import _check_agent_env_leakage

        agent_env = tmp_path / "agents" / "jira" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("JIRA_BASE_URL=https://example.atlassian.net\nJIRA_BACKEND=rest\n")

        warnings = _check_agent_env_leakage("jira", tmp_path)
        assert len(warnings) == 1
        assert "JIRA_BACKEND" in warnings[0]

    def test_no_warning_when_only_local_keys(self, tmp_path):
        """Agent .env with only local keys produces no warnings."""
        from framework.config import _check_agent_env_leakage

        agent_env = tmp_path / "agents" / "jira" / ".env"
        agent_env.parent.mkdir(parents=True)
        agent_env.write_text("JIRA_BASE_URL=https://example.atlassian.net\nJIRA_TOKEN=secret\n")

        warnings = _check_agent_env_leakage("jira", tmp_path)
        assert warnings == []

    def test_no_warning_when_env_file_absent(self, tmp_path):
        """Missing agent .env produces no warnings."""
        from framework.config import _check_agent_env_leakage

        warnings = _check_agent_env_leakage("jira", tmp_path)
        assert warnings == []

    def test_validate_startup_config_with_agent_id_checks_leakage(self, project_dir, monkeypatch, tmp_path):
        """validate_startup_config with agent_id warns on shared key in agent .env."""
        from framework.config import validate_startup_config

        # Create an agent .env with a shared selector key
        agent_env = project_dir / "agents" / "jira" / ".env"
        agent_env.parent.mkdir(parents=True, exist_ok=True)
        agent_env.write_text("JIRA_BASE_URL=https://x.atlassian.net\nJIRA_BACKEND=rest\n")

        monkeypatch.setenv("AGENT_RUNTIME", "connect-agent")
        monkeypatch.delenv("SCM_BASE_URL", raising=False)
        warnings = validate_startup_config(project_dir, skip_credential_check=True, agent_id="jira")
        assert any("JIRA_BACKEND" in w for w in warnings)

    def test_validate_startup_config_without_agent_id_skips_leakage(self, project_dir, monkeypatch):
        """validate_startup_config without agent_id does not check leakage."""
        from framework.config import validate_startup_config

        monkeypatch.setenv("AGENT_RUNTIME", "connect-agent")
        monkeypatch.delenv("SCM_BASE_URL", raising=False)
        # Should not raise even if no agent-level checks
        warnings = validate_startup_config(project_dir, skip_credential_check=True)
        assert isinstance(warnings, list)
