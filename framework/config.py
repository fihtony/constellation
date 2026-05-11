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
_ENV_OVERRIDES: dict[str, str] = {
    "CONSTELLATION_RUNTIME_BACKEND": "runtime.backend",
    "CONSTELLATION_RUNTIME_MODEL": "runtime.model",
    "OPENAI_MODEL": "runtime.model",
    "CONSTELLATION_REGISTRY_URL": "registry.url",
    "REGISTRY_URL": "registry.url",
    "CONSTELLATION_CONTAINER_RUNTIME": "container.runtime",
    "CONTAINER_RUNTIME": "container.runtime",
    "CONSTELLATION_DATA_DIR": "data.directory",
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
    cfg = load_agent_config(agent_id, project_root, overrides)
    data = cfg.to_dict()

    return {
        "agent_id": data.get("agent_id", agent_id),
        "name": data.get("name", agent_id),
        "description": data.get("description", ""),
        "version": data.get("version", "1.0.0"),
        "mode": data.get("mode", "task"),
        "execution_mode": data.get("execution_mode", "per-task"),
        "skills": data.get("default_skills", data.get("skills", [])),
        "tools": data.get("tools", []),
        "permissions": data.get("permissions", {}),
        "runtime_backend": data.get("runtime_backend", cfg.get("runtime.backend", "connect-agent")),
        "model": data.get("model", cfg.get("runtime.model", "gpt-5-mini")),
        "config": data,
    }
