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


# ---------------------------------------------------------------------------
# Isolated Git environment
# ---------------------------------------------------------------------------

# Env vars that carry Git credentials or affect credential discovery.
# These must be scrubbed before spawning git subprocesses in agents so that
# host keychains, user ~/.gitconfig, and system credential helpers are never
# consulted — only the explicit token passed via the authenticated URL.
_GIT_CREDENTIAL_VARS = frozenset({
    "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CREDENTIAL_HELPER",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    "GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN",
    "SCM_TOKEN", "SCM_USERNAME", "SCM_PASSWORD",
    "GCM_CREDENTIAL_STORE", "CREDENTIAL_HELPER",
    "HOME",  # prevents ~/.gitconfig from being read
})


def build_isolated_git_env(**extra: str) -> dict[str, str]:
    """Build a minimal subprocess environment for git operations.

    Strips host Git credential helpers, keychains, user ~/.gitconfig, and
    ambient GitHub tokens from the environment.  The caller passes the auth
    token via an authenticated remote URL instead.

    ``extra`` key/value pairs are merged in last (use to override PATH,
    GIT_TERMINAL_PROMPT=0, etc.).
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _GIT_CREDENTIAL_VARS:
            continue
        env[key] = value

    # Always disable interactive prompts in git subprocesses
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update(extra)
    return env
