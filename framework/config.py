"""Unified layered configuration loader.

Configuration is loaded in four layers with later layers overriding earlier ones:

1. **Global defaults** — ``config/constellation.yaml``
2. **Agent-specific** — ``agents/<agent>/config.yaml``
3. **Environment variables** — secrets and deployment overrides
4. **CLI / test overrides** — runtime-only short-term overrides

Merge rules:
- Scalars: last-write-wins (later layer overrides earlier).
- Dicts: deep merge (keys from later layer override matching keys).
- Lists: replace (later layer replaces entire list).
- Secrets: only from env or secret store, never written to YAML.

Usage::

    from framework.config import load_agent_config, load_global_config

    # Load merged config for a specific agent
    cfg = load_agent_config("team-lead")
    print(cfg["runtime"]["backend"])   # "connect-agent"

    # Load global config only
    global_cfg = load_global_config()
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge *override* into *base* (mutates *base*).

    - Dicts: recursive deep merge.
    - Lists: replace (no implicit merge).
    - Scalars: last-write-wins.
    """
    for key, value in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_yaml(path: str | Path) -> dict:
    """Load a YAML file. Returns empty dict if file does not exist or is empty."""
    import yaml  # type: ignore[import-untyped]

    path = Path(path)
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _find_project_root() -> Path:
    """Walk up from this file to find the project root (contains config/)."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / "config" / "constellation.yaml").is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback: assume two levels up from framework/
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Environment variable overlay
# ---------------------------------------------------------------------------

# Map of env var name → config path (dot-separated)
# NOTE: Order matters.  Generic / unprefixed vars are listed first so they
# can be overridden by the more-specific ``CONSTELLATION_*`` variants which
# come later (last-write-wins).
_ENV_OVERRIDES: dict[str, str] = {
    # --- unprefixed (lower priority) ---
    "AGENT_RUNTIME": "runtime.backend",
    "AGENT_MODEL": "runtime.model",
    "ANTHROPIC_MODEL": "runtime.model",
    "OPENAI_MODEL": "runtime.model",
    "REGISTRY_URL": "registry.url",
    "CONTAINER_RUNTIME": "container.runtime",
    # --- boundary backend selectors (global, placed in config/.env) ---
    "JIRA_BACKEND": "boundary.jira.backend",
    "SCM_BACKEND": "boundary.scm.backend",
    "UI_DESIGN_DEFAULT_PROVIDER": "boundary.ui_design.default_provider",
    # --- CONSTELLATION-prefixed (higher priority) ---
    "CONSTELLATION_RUNTIME_BACKEND": "runtime.backend",
    "CONSTELLATION_RUNTIME_MODEL": "runtime.model",
    "CONSTELLATION_REGISTRY_URL": "registry.url",
    "CONSTELLATION_CONTAINER_RUNTIME": "container.runtime",
    "CONSTELLATION_DATA_DIR": "data.directory",
    # --- identity / network (no priority conflict) ---
    "AGENT_ID": "agent_id",
    "PORT": "port",
    "HOST": "host",
}


def _apply_env_overrides(config: dict) -> dict:
    """Apply environment variable overrides to the config dict."""
    for env_var, config_path in _ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value is None:
            continue
        # Navigate the config path and set the value
        keys = config_path.split(".")
        target = config
        for key in keys[:-1]:
            if key not in target:
                target[key] = {}
            target = target[key]
        # Convert numeric strings
        if value.isdigit():
            value = int(value)  # type: ignore[assignment]
        target[keys[-1]] = value
    return config


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConstellationConfig:
    """Immutable configuration snapshot after all layers are merged."""

    data: dict = field(default_factory=dict)

    def get(self, path: str, default: Any = None) -> Any:
        """Get a value by dot-separated path (e.g., 'runtime.backend')."""
        keys = path.split(".")
        current: Any = self.data
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def to_dict(self) -> dict:
        """Return a shallow copy of the underlying data."""
        return dict(self.data)


def load_global_config(
    project_root: str | Path | None = None,
) -> ConstellationConfig:
    """Load the global ``config/constellation.yaml`` config.

    Returns an immutable ConstellationConfig snapshot.
    """
    root = Path(project_root) if project_root else _find_project_root()
    global_path = root / "config" / "constellation.yaml"
    data = _load_yaml(global_path)
    _apply_env_overrides(data)
    return ConstellationConfig(data=data)


def load_agent_config(
    agent_id: str,
    project_root: str | Path | None = None,
    overrides: dict | None = None,
) -> ConstellationConfig:
    """Load merged config: global defaults + agent-specific + env + overrides.

    Parameters
    ----------
    agent_id:
        Agent identifier (e.g., 'team-lead', 'compass').  Used to locate
        ``agents/<agent_dir>/config.yaml``.  Hyphens are converted to
        underscores for directory lookup.
    project_root:
        Explicit project root path.  Auto-detected if not provided.
    overrides:
        CLI / test overrides (layer 4).  Applied last.

    Returns
    -------
    ConstellationConfig
        Immutable snapshot of the merged configuration.
    """
    root = Path(project_root) if project_root else _find_project_root()

    # Layer 1: global defaults
    global_path = root / "config" / "constellation.yaml"
    merged = _load_yaml(global_path)

    # Layer 2: agent-specific config
    agent_dir = agent_id.replace("-", "_")
    agent_path = root / "agents" / agent_dir / "config.yaml"
    agent_data = _load_yaml(agent_path)
    if agent_data:
        _deep_merge(merged, agent_data)

    # Layer 3: environment variables
    _apply_env_overrides(merged)

    # Layer 4: CLI / test overrides
    if overrides:
        _deep_merge(merged, overrides)

    return ConstellationConfig(data=merged)


def build_agent_definition_from_config(
    agent_id: str,
    project_root: str | Path | None = None,
    overrides: dict | None = None,
) -> dict:
    """Build an AgentDefinition-compatible dict from the merged config.

    This bridges the config system and AgentDefinition so that agent.py
    files can derive their definitions from YAML instead of hardcoding.

    Returns a dict suitable for ``AgentDefinition(**result)``.
    """
    root = Path(project_root) if project_root else _find_project_root()
    cfg = load_agent_config(agent_id, root, overrides)
    data = cfg.to_dict()
    permission_profile = data.get("permission_profile", "")
    profile_permissions = {}
    if permission_profile:
        profile_permissions = _load_yaml(root / "config" / "permissions" / f"{permission_profile}.yaml")
    tools = data.get("tools", [])
    inline_permissions = data.get("permissions", {})
    permissions = dict(profile_permissions)
    if isinstance(inline_permissions, dict) and inline_permissions:
        _deep_merge(permissions, inline_permissions)
    if not tools and isinstance(permissions, dict):
        tools = list(permissions.get("allowed_tools", []) or [])

    return {
        "agent_id": data.get("agent_id", agent_id),
        "name": data.get("name", agent_id),
        "description": data.get("description", ""),
        "version": data.get("version", "1.0.0"),
        "mode": data.get("mode", "task"),
        "execution_mode": data.get("execution_mode", "per-task"),
        "skills": data.get("default_skills", data.get("skills", [])),
        "tools": tools,
        "permissions": permissions,
        "permission_profile": permission_profile,
        "runtime_backend": data.get("runtime_backend", cfg.get("runtime.backend", "connect-agent")),
        "model": data.get("model", cfg.get("runtime.model", "gpt-5-mini")),
        "config": data,
        "launch_spec": data.get("launch_spec") or data.get("launchSpec"),
    }


# ---------------------------------------------------------------------------
# Boundary config helpers
# ---------------------------------------------------------------------------

def get_boundary_backend(
    domain: str,
    project_root: str | Path | None = None,
) -> str:
    """Return the configured backend for a boundary domain.

    Resolution order (last-write-wins):
    1. ``config/constellation.yaml``  boundary.<domain>.backend  (or default_provider)
    2. Environment variable (``JIRA_BACKEND``, ``SCM_BACKEND``, ``UI_DESIGN_DEFAULT_PROVIDER``)

    Parameters
    ----------
    domain:
        One of ``'jira'``, ``'scm'``, ``'ui_design'``.
    """
    cfg = load_global_config(project_root)
    key_map = {
        "jira": ("boundary.jira.backend", "mcp"),
        "scm": ("boundary.scm.backend", "github-mcp"),
        "ui_design": ("boundary.ui_design.default_provider", "stitch"),
    }
    if domain not in key_map:
        raise ValueError(f"Unknown boundary domain: {domain!r}. Expected: jira, scm, ui_design")
    config_path, default = key_map[domain]
    return cfg.get(config_path, default)


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

class ConfigValidationError(Exception):
    """Raised when startup configuration is inconsistent or incomplete."""


# Keys that are global deployment selectors and must NOT be redefined in
# agent-level .env files.
_SHARED_SELECTOR_KEYS: frozenset[str] = frozenset({
    "JIRA_BACKEND",
    "SCM_BACKEND",
    "UI_DESIGN_DEFAULT_PROVIDER",
    "AGENT_RUNTIME",
    "AGENT_MODEL",
    "CONTAINER_RUNTIME",
})


def _check_agent_env_leakage(
    agent_id: str,
    project_root: Path,
) -> list[str]:
    """Return warnings for shared selector keys found in an agent-level .env file."""
    agent_dir = agent_id.replace("-", "_")
    agent_env = project_root / "agents" / agent_dir / ".env"
    if not agent_env.is_file():
        return []
    warnings: list[str] = []
    with open(agent_env, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key in _SHARED_SELECTOR_KEYS:
                warnings.append(
                    f"Shared selector key {key!r} is defined in agents/{agent_dir}/.env "
                    f"but should only be in config/.env. Remove it from the agent-level "
                    f"file to avoid configuration conflicts."
                )
    return warnings


def validate_startup_config(
    project_root: str | Path | None = None,
    *,
    skip_credential_check: bool = False,
    agent_id: str | None = None,
) -> list[str]:
    """Validate the merged configuration for consistency.

    Performs fail-fast checks so misconfiguration is caught at startup rather
    than at first runtime call.

    Parameters
    ----------
    project_root:
        Project root directory.  Auto-detected if not provided.
    skip_credential_check:
        When True, skip checks that require credentials to be present in the
        environment (useful for boundary agents that don't use shared runtime).
    agent_id:
        When provided, also checks the agent-level .env for leaked shared
        selector keys and returns warnings for each one found.

    Returns
    -------
    list[str]
        Empty list on success.  Non-empty list of warning strings for
        non-fatal issues (e.g., optional providers missing credentials).

    Raises
    ------
    ConfigValidationError
        On fatal configuration errors (conflicting backend/URL, missing
        required credentials).
    """
    cfg = load_global_config(project_root)
    errors: list[str] = []
    warnings: list[str] = []

    # ------------------------------------------------------------------
    # SCM backend vs URL consistency
    # ------------------------------------------------------------------
    scm_backend = cfg.get("boundary.scm.backend", "github-mcp")
    scm_base_url = os.environ.get("SCM_BASE_URL", "").lower().strip()

    if scm_base_url:
        if scm_backend == "bitbucket" and "github.com" in scm_base_url:
            errors.append(
                f"SCM_BACKEND=bitbucket but SCM_BASE_URL points to GitHub ({scm_base_url!r}). "
                "Set SCM_BACKEND=github-rest or github-mcp, or correct SCM_BASE_URL."
            )
        if scm_backend in ("github-rest", "github-mcp") and any(
            pat in scm_base_url for pat in ("bitbucket.", ".bitbucket.")
        ):
            errors.append(
                f"SCM_BACKEND={scm_backend!r} but SCM_BASE_URL points to Bitbucket ({scm_base_url!r}). "
                "Set SCM_BACKEND=bitbucket, or correct SCM_BASE_URL."
            )

    # ------------------------------------------------------------------
    # UI Design provider vs credentials
    # ------------------------------------------------------------------
    if not skip_credential_check:
        ui_provider = cfg.get("boundary.ui_design.default_provider", "stitch")
        if ui_provider == "figma" and not os.environ.get("FIGMA_TOKEN", "").strip():
            errors.append(
                "UI_DESIGN_DEFAULT_PROVIDER=figma but FIGMA_TOKEN is not set. "
                "Set FIGMA_TOKEN in agents/ui_design/.env."
            )
        if ui_provider == "stitch" and not os.environ.get("STITCH_API_KEY", "").strip():
            warnings.append(
                "UI_DESIGN_DEFAULT_PROVIDER=stitch but STITCH_API_KEY is not set. "
                "Set STITCH_API_KEY in agents/ui_design/.env if you use UI design capabilities."
            )

    # ------------------------------------------------------------------
    # Agentic runtime vs credentials
    # ------------------------------------------------------------------
    if not skip_credential_check:
        runtime = cfg.get("runtime.backend", "claude-code")
        if runtime == "claude-code":
            if not os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip():
                errors.append(
                    "AGENT_RUNTIME=claude-code but ANTHROPIC_AUTH_TOKEN is not set. "
                    "Set ANTHROPIC_AUTH_TOKEN in config/.env."
                )
        elif runtime == "connect-agent":
            # CONNECT_AGENT_URL has a valid runtime default, so missing is only a warning
            if not os.environ.get("CONNECT_AGENT_URL", "").strip():
                warnings.append(
                    "AGENT_RUNTIME=connect-agent and CONNECT_AGENT_URL is not set; "
                    "runtime default will be used (http://localhost:1288 or container equivalent)."
                )
        elif runtime == "copilot-cli":
            if not os.environ.get("COPILOT_GITHUB_TOKEN", "").strip():
                errors.append(
                    "AGENT_RUNTIME=copilot-cli but COPILOT_GITHUB_TOKEN is not set. "
                    "Set COPILOT_GITHUB_TOKEN in config/.env."
                )

    # ------------------------------------------------------------------
    # Container runtime
    # ------------------------------------------------------------------
    container_runtime = cfg.get("container.runtime", "docker")
    if container_runtime not in ("docker", "rancher"):
        errors.append(
            f"CONTAINER_RUNTIME={container_runtime!r} is not valid. "
            "Supported values: docker | rancher."
        )

    # ------------------------------------------------------------------
    # Shared selector leakage in agent-level .env
    # ------------------------------------------------------------------
    if agent_id:
        root = Path(project_root) if project_root else _find_project_root()
        warnings.extend(_check_agent_env_leakage(agent_id, root))

    if errors:
        raise ConfigValidationError("\n".join(errors))

    return warnings
