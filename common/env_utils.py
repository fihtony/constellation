"""Helpers for loading agent-local .env files and resolving booleans."""

from __future__ import annotations

import os


def _load_env_file(path, loaded):
    if not path or not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded[key] = value


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

    Existing environment variables are preserved. Blank lines and comments are ignored.
    """
    loaded = {}
    for candidate in _candidate_env_files(path):
        _load_env_file(candidate, loaded)
    return loaded


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}