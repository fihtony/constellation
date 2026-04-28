"""Helpers for loading shared and agent-local .env files and resolving booleans."""

from __future__ import annotations

import os
import re
import tempfile


_CONTAINER_RUNTIME_HOSTS = {
    "docker": "host.docker.internal",
    "rancher": "host.rancher-desktop.internal",
}
_TRUSTED_ENV_OVERRIDE_FLAG = "CONSTELLATION_TRUSTED_ENV"
_PROTECTED_ENV_KEYS = frozenset({
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "COPILOT_GITHUB_TOKEN",
    "SCM_TOKEN",
    "SCM_USERNAME",
    "SCM_PASSWORD",
    "TEST_GITHUB_TOKEN",
})

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


def sanitize_credential_env(base_env=None, *, keep=None):
    """Return a copy of env with host GitHub/SCM credentials stripped.

    Callers can re-inject explicitly trusted file-backed credentials through
    ``keep``.
    """
    env = dict(base_env or os.environ)
    for key in _PROTECTED_ENV_KEYS:
        env.pop(key, None)
    for key, value in (keep or {}).items():
        if value is None:
            continue
        text = str(value)
        if text.strip():
            env[key] = text
    return env


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
    1. Trusted process environment values when
       CONSTELLATION_TRUSTED_ENV=1
    2. Agent-local .env
    3. common/.env shared defaults

    GitHub/SCM credential variables are file-backed by default: ambient host
    values are ignored unless the caller marks the current process environment
    as trusted (for example a launcher or a test that already loaded values from
    its own .env file).

    Blank lines and comments are ignored. The returned mapping contains the merged
    file-backed configuration (shared defaults plus agent-local overrides), which
    callers such as the container launcher can forward into child processes.
    """
    original_env = dict(os.environ)
    trusted_override = original_env.get(_TRUSTED_ENV_OVERRIDE_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}
    merged = {}
    for candidate in _candidate_env_files(path):
        _load_env_file(candidate, merged)

    for key, value in merged.items():
        if key in _PROTECTED_ENV_KEYS:
            continue
        if original_env.get(key, "").strip():
            continue
        os.environ[key] = value

    for key in _PROTECTED_ENV_KEYS:
        current_value = original_env.get(key, "")
        file_value = merged.get(key, "")
        if trusted_override and str(current_value).strip():
            os.environ[key] = current_value
            continue
        if str(file_value).strip():
            os.environ[key] = file_value
            continue
        os.environ.pop(key, None)

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
    env = sanitize_credential_env(base_env)
    home = isolated_runtime_home(scope)
    xdg_config_home = os.path.join(home, ".config")
    os.makedirs(xdg_config_home, exist_ok=True)
    # Write a minimal isolated git config that trusts all directories (safe.directory=*)
    # so bind-mounted workspaces with a different owner UID don't trigger "dubious ownership".
    # credential.helper is explicitly cleared to prevent any credential leakage.
    git_config_path = os.path.join(home, ".gitconfig-isolated")
    if not os.path.exists(git_config_path):
        with open(git_config_path, "w", encoding="utf-8") as fh:
            fh.write("[safe]\n\tdirectory = *\n[credential]\n\thelper =\n")
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
    return env


def build_isolated_copilot_env(token, base_env=None):
    env = sanitize_credential_env(base_env, keep={"COPILOT_GITHUB_TOKEN": token})
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
    return env


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}