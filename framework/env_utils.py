"""Environment utilities — URL resolution, container detection, .env loading."""
from __future__ import annotations

import os
import re
import tempfile

_CONTAINER_RUNTIME_HOSTS = {
    "docker": "host.docker.internal",
    "rancher": "host.rancher-desktop.internal",
}

_ISOLATED_RUNTIME_ROOT = os.path.join(tempfile.gettempdir(), "constellation-runtime")


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file (ignoring comments)."""
    values: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    return values


def load_dotenv(path: str) -> None:
    """Load a .env file into ``os.environ`` (does not override existing keys)."""
    for key, value in _parse_env_file(path).items():
        os.environ.setdefault(key, value)


def resolve_container_runtime(default: str = "docker") -> str:
    runtime = (os.environ.get("CONTAINER_RUNTIME") or default).strip().lower()
    return "rancher" if runtime == "rancher" else "docker"


def _is_containerized() -> bool:
    return any((
        bool(os.environ.get("CONTAINER_ID", "").strip()),
        os.path.exists("/.dockerenv"),
        os.path.exists("/run/.containerenv"),
    ))


def default_openai_base_url() -> str:
    host = "localhost"
    if _is_containerized():
        host = _CONTAINER_RUNTIME_HOSTS[resolve_container_runtime()]
    return f"http://{host}:1288/v1"


def resolve_openai_base_url() -> str:
    """Resolve the OpenAI-compatible API base URL."""
    explicit = os.environ.get("OPENAI_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return default_openai_base_url()


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean flag from an environment variable."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
