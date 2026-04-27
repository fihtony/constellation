"""Helpers for loading shared and agent-local .env files and resolving booleans."""

from __future__ import annotations

import os


def _parse_env_file(path):
    values = {}
    if not path or not os.path.exists(path):
        return values

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    return values


def _load_env_file(path, merged):
    merged.update(_parse_env_file(path))


def _candidate_env_files(path):
    if not path:
        return []
    abs_path = os.path.abspath(path)
    repo_root = os.path.dirname(os.path.dirname(abs_path))
    shared_env = os.path.join(repo_root, "common", ".env")
    if shared_env == abs_path:
        return [abs_path]
    return [shared_env, abs_path]


def load_dotenv(path):
    """Load a simple KEY=VALUE dotenv file into the process environment.

    Precedence:
    1. Existing non-empty process environment values
    2. Agent-local .env
    3. common/.env shared defaults

    Blank lines and comments are ignored. The returned mapping contains the merged
    file-backed configuration (shared defaults plus agent-local overrides), which
    callers such as the container launcher can forward into child processes.
    """
    original_env = dict(os.environ)
    merged = {}
    for candidate in _candidate_env_files(path):
        _load_env_file(candidate, merged)

    for key, value in merged.items():
        if original_env.get(key, "").strip():
            continue
        os.environ[key] = value

    return merged


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}