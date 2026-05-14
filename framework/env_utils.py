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
# Stripped before spawning git subprocesses so that host keychains,
# user ~/.gitconfig credential-helper entries, and ambient GitHub tokens
# are never consulted.  Auth is passed explicitly via http.extraHeader.
_GIT_CREDENTIAL_VARS = frozenset({
    "HOME", "XDG_CONFIG_HOME",                     # override with isolated dirs
    "GIT_ASKPASS", "SSH_ASKPASS",
    "GIT_CREDENTIAL_HELPER", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    "GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN",
    "SCM_TOKEN", "SCM_USERNAME", "SCM_PASSWORD",
    "GCM_CREDENTIAL_STORE", "CREDENTIAL_HELPER",
    "GCM_INTERACTIVE",
})


def isolated_runtime_home(scope: str = "git") -> str:
    """Return a per-scope temporary HOME directory for git subprocesses.

    The directory is created if it does not exist.  Using a per-scope HOME
    ensures git never reads the host user's ~/.gitconfig or macOS Keychain.
    """
    import re
    safe_scope = re.sub(r"[^a-zA-Z0-9_-]", "-", scope).strip("-") or "default"
    home = os.path.join(_ISOLATED_RUNTIME_ROOT, safe_scope)
    os.makedirs(home, exist_ok=True)
    return home


def build_isolated_git_env(scope: str = "git", **extra: str) -> dict[str, str]:
    """Build a fully isolated subprocess environment for git operations.

    Strategy (mirrors v1 common/env_utils.py):
    - Strip all credential and HOME-related env vars.
    - Set HOME to a per-scope temp directory so git cannot reach
      ~/.gitconfig, macOS Keychain, or any OS credential helper.
    - Write a minimal .gitconfig-isolated that disables credential helpers
      and trusts all directories (avoids dubious-ownership errors on
      bind-mounted workspaces).
    - Set GIT_CONFIG_GLOBAL to that file and GIT_CONFIG_NOSYSTEM=1 so
      /etc/gitconfig is also ignored.
    - Disable all interactive prompts.

    The caller injects auth via ``git -c http.extraHeader=Authorization: ...``
    in the command itself — credentials never appear in the remote URL.

    ``scope`` differentiates the isolated HOME directory per agent/workflow.
    ``extra`` key/value pairs are merged in last.
    """
    # Start from process env, strip credential vars
    env: dict[str, str] = {k: v for k, v in os.environ.items()
                            if k not in _GIT_CREDENTIAL_VARS}

    # Isolated HOME — no host ~/.gitconfig or OS credential helper
    home = isolated_runtime_home(scope)
    xdg_config_home = os.path.join(home, ".config")
    os.makedirs(xdg_config_home, exist_ok=True)

    # Write a minimal isolated gitconfig once per scope
    git_config_path = os.path.join(home, ".gitconfig-isolated")
    if not os.path.exists(git_config_path):
        with open(git_config_path, "w", encoding="utf-8") as fh:
            fh.write(
                "[safe]\n\tdirectory = *\n"
                "[credential]\n\thelper =\n"
            )

    env.update({
        "HOME": home,
        "XDG_CONFIG_HOME": xdg_config_home,
        "GIT_CONFIG_GLOBAL": git_config_path,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
    })
    env.update(extra)
    return env
