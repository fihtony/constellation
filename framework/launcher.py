"""Launch per-task agent containers through a Docker-compatible socket."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import http.client
import json
import os
import socket
import time
import uuid
from urllib.parse import quote


_CHILD_SOCKET_PATH = "/var/run/docker.sock"

# Role taxonomy emitted as the `constellation.agent_role` container
# label. Three values are recognised:
#
#   "orchestrator" — long-running control-plane agents (compass,
#                    team_lead). These hold the docker socket and
#                    may launch child agents.
#   "on-demand"    — task executors spawned per task and torn down
#                    afterwards (office, web-dev, code-review,
#                    future android-dev / ios-dev). They must NEVER
#                    receive the docker socket.
#   "boundary"     — long-running integration adapters (jira, scm,
#                    ui_design). They expose fixed services and
#                    do not launch children.
#
# The ExecutionMode enum keeps the canonical value "per-task" for
# backwards compatibility with downstream consumers (registry
# store, scripts, tests); the label emitted to docker is the
# friendlier "on-demand".
ON_DEMAND_ROLE_LABEL = "on-demand"


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


def _as_dict(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _spec_value(spec: dict, *names: str, default=None):
    for name in names:
        if name in spec and spec[name] is not None:
            return spec[name]
    return default


def _enum_value(value, default: str = "") -> str:
    if value is None:
        return default
    if hasattr(value, "value"):
        value = value.value
    text = str(value).strip()
    return text or default


def _parse_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    return values


def _parse_memory_bytes(memory_str: str) -> int:
    s = memory_str.strip().lower().rstrip("b")
    multipliers = {"g": 1024 ** 3, "m": 1024 ** 2, "k": 1024}
    for suffix, multiplier in multipliers.items():
        if s.endswith(suffix):
            try:
                return int(s[:-1]) * multiplier
            except ValueError:
                return 0
    try:
        return int(s)
    except ValueError:
        return 0


def _child_path(parent: str, child: str) -> str:
    parent = (parent or "").rstrip(os.sep)
    child = (child or "").strip().strip(os.sep)
    if not parent:
        return child
    if not child:
        return parent
    return os.path.join(parent, child)


def _ensure_task_workspace_dir(container_path: str, host_path: str) -> None:
    """Create the task workspace directory from the current runtime's visible path.

    When Launcher runs inside a container, *host_path* points at a host-only path
    (for example `/Users/...`) that the current process cannot write directly.
    Creating the bind-mounted *container_path* ensures the directory materializes
    on the host through the existing `/app/artifacts` bind mount. When Launcher
    runs on the host, creating *container_path* usually fails, so we fall back to
    *host_path*.
    """

    candidates: list[str] = []
    for candidate in (container_path, host_path):
        value = str(candidate or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
            return
        except OSError:
            continue


class Launcher:
    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or os.environ.get("DOCKER_SOCKET", _CHILD_SOCKET_PATH)
        self.runtime_image = os.environ.get("DEFAULT_DYNAMIC_IMAGE", "")
        self.runtime_network = os.environ.get("DYNAMIC_AGENT_NETWORK", "constellation-v2-network")
        self.registry_url = os.environ.get("REGISTRY_URL", "http://registry:9000")
        self.default_port = int(os.environ.get("DYNAMIC_AGENT_PORT", "8000"))

    def _request_raw(self, method: str, path: str, payload=None):
        if not os.path.exists(self.socket_path):
            raise RuntimeError(f"Docker socket not available at {self.socket_path}")

        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        conn = UnixSocketHTTPConnection(self.socket_path)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
        finally:
            conn.close()

        return response.status, raw

    def _request(self, method: str, path: str, payload=None):
        status, raw = self._request_raw(method, path, payload=payload)
        if status >= 400:
            raise RuntimeError(f"Docker API {method} {path} failed: HTTP {status}: {raw}")
        if not raw:
            return None
        return json.loads(raw)

    def _current_container_mounts(self) -> list[dict]:
        container_id = (
            os.environ.get("CONTAINER_ID", "").strip()
            or os.environ.get("HOSTNAME", "").strip()
        )
        if not container_id:
            return []
        try:
            safe_id = quote(container_id, safe="")
            status, raw = self._request_raw("GET", f"/v1.43/containers/{safe_id}/json")
            if status >= 400 or not raw:
                return []
            data = json.loads(raw)
            mounts = data.get("Mounts") or []
            return mounts if isinstance(mounts, list) else []
        except Exception:
            return []

    def resolve_host_path(self, container_path: str) -> str:
        if not container_path:
            return ""

        target_real = os.path.realpath(container_path)
        mounts = self._current_container_mounts()
        best_match: tuple[int, str] | None = None

        for mount in mounts:
            destination = str(mount.get("Destination") or "")
            source = str(mount.get("Source") or "")
            if not destination or not source:
                continue
            destination_real = os.path.realpath(destination)
            prefix = destination_real.rstrip(os.sep) + os.sep
            if target_real == destination_real:
                score = len(destination_real)
                candidate = source
            elif target_real.startswith(prefix):
                relative = os.path.relpath(target_real, destination_real)
                candidate = os.path.realpath(os.path.join(source, relative))
                score = len(destination_real)
            else:
                continue

            if best_match is None or score > best_match[0]:
                best_match = (score, candidate)

        if best_match is not None:
            return best_match[1]
        return target_real

    def resolve_container_path(self, host_path: str) -> str:
        if not host_path:
            return ""

        target_real = os.path.realpath(host_path)
        mounts = self._current_container_mounts()
        best_match: tuple[int, str] | None = None

        for mount in mounts:
            destination = str(mount.get("Destination") or "")
            source = str(mount.get("Source") or "")
            if not destination or not source:
                continue
            source_real = os.path.realpath(source)
            prefix = source_real.rstrip(os.sep) + os.sep
            if target_real == source_real:
                score = len(source_real)
                candidate = destination
            elif target_real.startswith(prefix):
                relative = os.path.relpath(target_real, source_real)
                candidate = os.path.realpath(os.path.join(destination, relative))
                score = len(source_real)
            else:
                continue

            if best_match is None or score > best_match[0]:
                best_match = (score, candidate)

        if best_match is not None:
            return best_match[1]
        return target_real

    def launch_instance(self, agent_definition, task_id: str, *, launch_overrides: dict | None = None) -> dict:
        definition = _as_dict(agent_definition)
        base_spec = _as_dict(definition.get("launch_spec") or definition.get("launchSpec"))
        if not base_spec:
            raise NotImplementedError(
                f"Agent '{definition.get('agent_id', 'unknown')}' does not define launch_spec for per-task startup."
            )

        spec = dict(base_spec)
        overrides = launch_overrides or {}

        # On-demand agents must never receive the docker socket — they
        # are spawned as task executors and have no business launching
        # further containers. Strip socket-mount directives from the
        # base spec (defence in depth: the launch_spec shouldn't ask
        # for it in the first place) and reject any override that tries
        # to opt back in. Only the orchestrator agents are allowed to
        # carry the docker socket.
        execution_mode_text = str(
            _enum_value(definition.get("execution_mode"), "")
        ).strip().lower()
        if execution_mode_text in {"per-task", "on-demand"}:
            spec.pop("mount_docker_socket", None)
            spec.pop("mountDockerSocket", None)
            if (
                overrides.get("mount_docker_socket")
                or overrides.get("mountDockerSocket")
            ):
                raise PermissionError(
                    f"Refusing to launch on-demand agent "
                    f"'{definition.get('agent_id', '?')}': "
                    f"launch_overrides requested docker socket mount, "
                    f"which is forbidden for on-demand agents."
                )

        if overrides.get("env"):
            spec["env"] = {
                **_as_dict(spec.get("env")),
                **_as_dict(overrides.get("env")),
            }
        if overrides.get("extra_binds"):
            spec["extra_binds"] = list(overrides.get("extra_binds") or [])
        for key, value in overrides.items():
            if key in {"env", "extra_binds"}:
                continue
            spec[key] = value

        agent_id = _enum_value(definition.get("agent_id"), "unknown-agent")
        container_prefix = str(_spec_value(spec, "name_prefix", "namePrefix", default=agent_id.replace("_", "-"))).strip()
        unique_suffix = uuid.uuid4().hex[:8]
        container_name = f"{container_prefix}-{task_id.lower()}-{unique_suffix}"
        port = int(_spec_value(spec, "port", default=self.default_port) or self.default_port)
        service_url = f"http://{container_name}:{port}"
        image = str(_spec_value(spec, "image", default=self.runtime_image) or "").strip()
        if not image:
            raise RuntimeError(f"No image configured for per-task agent '{agent_id}'")

        env = {}
        env_file = str(_spec_value(spec, "env_file", "envFile", default="") or "").strip()
        if env_file:
            env.update(_parse_env_file(env_file))
        for key in list(_spec_value(spec, "pass_through_env", "passThroughEnv", default=[]) or []):
            value = os.environ.get(str(key))
            if value is not None:
                env[str(key)] = value
        for key, value in _as_dict(_spec_value(spec, "env", default={}) or {}).items():
            env[str(key)] = str(value)

        env.update({
            "HOST": "0.0.0.0",
            "PORT": str(port),
            "AGENT_ID": agent_id,
            "REGISTRY_URL": self.registry_url,
            "ADVERTISED_BASE_URL": service_url,
            "CONTAINER_ID": container_name,
            "CONSTELLATION_TRUSTED_ENV": "1",
        })

        payload = {
            "Image": image,
            "Env": [f"{key}={value}" for key, value in sorted(env.items())],
            "Labels": {
                "constellation.agent_id": agent_id,
                "constellation.agent_name": str(definition.get("name") or agent_id),
                "constellation.agent_role": _enum_value(definition.get("execution_mode"), ON_DEMAND_ROLE_LABEL),
                "constellation.task_id": task_id,
            },
            "HostConfig": {
                "AutoRemove": True,
            },
            "NetworkingConfig": {
                "EndpointsConfig": {
                    self.runtime_network: {}
                }
            },
        }

        command = _spec_value(spec, "command", "cmd", default=None)
        if command:
            payload["Cmd"] = list(command)

        platform = str(_spec_value(spec, "platform", default="") or "").strip()
        if platform:
            payload["Platform"] = platform

        memory_str = str(_spec_value(spec, "memory", default="") or "").strip().lower()
        if memory_str:
            memory_bytes = _parse_memory_bytes(memory_str)
            if memory_bytes > 0:
                payload["HostConfig"]["Memory"] = memory_bytes
                payload["HostConfig"]["MemorySwap"] = memory_bytes

        binds: list[str] = []
        mount_artifact_root = bool(_spec_value(spec, "mount_artifact_root", "mountArtifactRoot", default=True))
        if mount_artifact_root:
            artifact_root_container = os.environ.get("ARTIFACT_ROOT", "/app/artifacts")
            task_workspace_container = _child_path(artifact_root_container, task_id)
            artifact_root_host = self.resolve_host_path(artifact_root_container)
            task_workspace_host = self.resolve_host_path(task_workspace_container)
            if artifact_root_host and (not task_workspace_host or task_workspace_host == task_workspace_container):
                task_workspace_host = _child_path(artifact_root_host, task_id)
            if task_workspace_host:
                _ensure_task_workspace_dir(task_workspace_container, task_workspace_host)
                binds.append(f"{task_workspace_host}:{task_workspace_container}")
                env["ARTIFACT_ROOT"] = artifact_root_container
                env["CONSTELLATION_TASK_WORKSPACE"] = task_workspace_container
                payload["Env"] = [f"{key}={value}" for key, value in sorted(env.items())]

        mount_socket = bool(_spec_value(spec, "mount_docker_socket", "mountDockerSocket", default=False))
        if mount_socket and os.path.exists(self.socket_path):
            host_socket_path = self.resolve_host_path(self.socket_path)
            if host_socket_path:
                binds.append(f"{host_socket_path}:{_CHILD_SOCKET_PATH}")
                group_add = self._socket_group_add(self.socket_path)
                if group_add:
                    payload["HostConfig"]["GroupAdd"] = group_add
                env["DOCKER_SOCKET"] = _CHILD_SOCKET_PATH
                payload["Env"] = [f"{key}={value}" for key, value in sorted(env.items())]

        for bind in list(_spec_value(spec, "extra_binds", "extraBinds", default=[]) or []):
            bind_text = str(bind or "").strip()
            if bind_text:
                binds.append(bind_text)

        if binds:
            payload["HostConfig"]["Binds"] = binds

        self._request(
            "POST",
            f"/v1.43/containers/create?name={quote(container_name, safe='')}",
            payload=payload,
        )
        self._request("POST", f"/v1.43/containers/{quote(container_name, safe='')}/start")
        time.sleep(float(_spec_value(spec, "startup_delay_seconds", "startupDelaySeconds", default=1.0) or 1.0))
        return {
            "container_name": container_name,
            "service_url": service_url,
            "port": port,
        }

    def destroy_instance(self, agent_id: str, container_name: str) -> None:
        self._request("DELETE", f"/v1.43/containers/{quote(container_name, safe='')}?force=1")

    def find_live_instances(self, agent_id: str, task_id: str = "") -> list[dict]:
        """Find running containers for a given agent_id (and optionally task_id).

        Returns a list of dicts with container_name, service_url, task_id.
        Used for duplicate instance prevention.
        """
        filters = {"label": [f"constellation.agent_id={agent_id}"], "status": ["running"]}
        if task_id:
            filters["label"].append(f"constellation.task_id={task_id}")
        filters_json = json.dumps(filters)
        try:
            status, raw = self._request_raw(
                "GET",
                f"/v1.43/containers/json?filters={quote(filters_json, safe='')}",
            )
            if status >= 400 or not raw:
                return []
            containers = json.loads(raw)
            results = []
            for c in containers:
                labels = c.get("Labels") or {}
                names = c.get("Names") or []
                name = names[0].lstrip("/") if names else ""
                results.append({
                    "container_name": name,
                    "task_id": labels.get("constellation.task_id", ""),
                    "agent_id": labels.get("constellation.agent_id", ""),
                })
            return results
        except Exception:
            return []

    def _socket_group_add(self, socket_path: str) -> list[str]:
        """Return the numeric GID(s) to attach to per-task containers so the
        non-root user can access the mounted docker socket.

        Tries to read the GID from the host socket file first.  When the
        host socket lives on a filesystem that does not support ``stat``
        (notably macOS's ``Socket`` filesystem used by Rancher Desktop's
        SSH-forwarded socket), it falls back to ``self.socket_gid`` which
        subclasses — most importantly :class:`RancherLauncher` — can
        override to ship a runtime-specific default.  On Docker Desktop
        the socket is on APFS so the stat-based path returns the real GID
        (typically 0 for root).
        """
        try:
            socket_gid = os.stat(socket_path).st_gid
            return [str(socket_gid)]
        except OSError:
            pass
        fallback = getattr(self, "socket_gid", 0)
        if fallback is None or fallback < 0:
            return []
        return [str(fallback)]


class RancherLauncher(Launcher):
    """Launcher tuned for Rancher Desktop on macOS / Linux.

    The host socket is forwarded by Lima over SSH, which on macOS places
    the file on a ``Socket`` filesystem that does not support ``stat``.
    The actual GID of the forwarded socket cannot be discovered at
    runtime, so the launcher falls back to ``DOCKER_SOCKET_GID`` from the
    environment (default ``102``, the standard ``docker`` group GID).
    Operators that have chgrp'd the socket to a non-default group can
    override via env before invoking docker compose.
    """

    DEFAULT_SOCKET_GID = 102

    def __init__(self):
        default_socket = _CHILD_SOCKET_PATH if os.path.exists(_CHILD_SOCKET_PATH) else os.path.expanduser("~/.rd/docker.sock")
        super().__init__(socket_path=os.environ.get("DOCKER_SOCKET", default_socket))
        try:
            self.socket_gid = int(
                os.environ.get("DOCKER_SOCKET_GID", str(self.DEFAULT_SOCKET_GID))
            )
        except ValueError:
            self.socket_gid = self.DEFAULT_SOCKET_GID


def get_launcher():
    runtime = (os.environ.get("CONTAINER_RUNTIME") or "docker").strip().lower()
    if runtime == "rancher":
        return RancherLauncher()
    return Launcher()