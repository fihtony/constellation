"""Helpers for loading shared and agent-local .env files and resolving booleans."""

from __future__ import annotations

import os
import re
import tempfile


_CONTAINER_RUNTIME_HOSTS = {
    "docker": "host.docker.internal",
    "rancher": "host.rancher-desktop.internal",
}

_ISOLATED_RUNTIME_ROOT = os.path.join(tempfile.gettempdir(), "constellation-runtime")


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


def resolve_container_runtime(default="docker"):
    runtime = (os.environ.get("CONTAINER_RUNTIME") or default).strip().lower()
    if runtime == "rancher":
        return "rancher"
    return "docker"


def _is_containerized_process():
    return any((
        bool(os.environ.get("CONTAINER_ID", "").strip()),
        os.path.exists("/.dockerenv"),
        os.path.exists("/run/.containerenv"),
    ))


def default_openai_base_url():
    host = "localhost"
    if _is_containerized_process():
        host = _CONTAINER_RUNTIME_HOSTS[resolve_container_runtime()]
    return f"http://{host}:1288/v1"


def resolve_openai_base_url():
    explicit = os.environ.get("OPENAI_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return default_openai_base_url()


def isolated_runtime_home(scope="default"):
    safe_scope = re.sub(r"[^A-Za-z0-9._-]+", "-", (scope or "default").strip())
    safe_scope = safe_scope.strip(".-") or "default"
    home = os.path.join(_ISOLATED_RUNTIME_ROOT, safe_scope)
    os.makedirs(home, exist_ok=True)
    return home


def build_isolated_git_env(base_env=None, *, scope="git"):
    env = dict(base_env or os.environ)
    home = isolated_runtime_home(scope)
    xdg_config_home = os.path.join(home, ".config")
    os.makedirs(xdg_config_home, exist_ok=True)
    env.update({
        "HOME": home,
        "XDG_CONFIG_HOME": xdg_config_home,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
    })
    # Git itself does not use these, but strip them so no credential helper or
    # git hook can accidentally pick up host GitHub tokens.
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return env


def build_isolated_copilot_env(token, base_env=None):
    env = dict(base_env or os.environ)
    home = isolated_runtime_home("copilot-cli")
    xdg_config_home = os.path.join(home, ".config")
    copilot_home = env.get("COPILOT_HOME", "").strip() or os.path.join(home, ".copilot")
    gh_config_dir = os.path.join(xdg_config_home, "gh")
    os.makedirs(xdg_config_home, exist_ok=True)
    os.makedirs(copilot_home, exist_ok=True)
    os.makedirs(gh_config_dir, exist_ok=True)
    env.update({
        "HOME": home,
        "XDG_CONFIG_HOME": xdg_config_home,
        "GH_CONFIG_DIR": gh_config_dir,
        "COPILOT_HOME": copilot_home,
        "COPILOT_GITHUB_TOKEN": token,
        "GCM_INTERACTIVE": "never",
    })
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return env


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}