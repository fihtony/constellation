"""Helpers for loading agent-local .env files and resolving booleans."""

from __future__ import annotations

import os


def load_dotenv(path):
    """Load a simple KEY=VALUE dotenv file into the process environment.

    Existing environment variables are preserved. Blank lines and comments are ignored.
    """
    if not path or not os.path.exists(path):
        return {}

    loaded = {}
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
    return loaded


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}