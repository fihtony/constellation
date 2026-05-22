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

    def launch_instance(self, agent_definition, task_id: str, *, launch_overrides: dict | None = None) -> dict:
        definition = _as_dict(agent_definition)
        base_spec = _as_dict(definition.get("launch_spec") or definition.get("launchSpec"))
        if not base_spec:
            raise NotImplementedError(
                f"Agent '{definition.get('agent_id', 'unknown')}' does not define launch_spec for per-task startup."
            )

        spec = dict(base_spec)
        overrides = launch_overrides or {}
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
                "constellation.agent_role": _enum_value(definition.get("execution_mode"), "per-task"),
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
        artifact_root_container = os.environ.get("ARTIFACT_ROOT", "/app/artifacts")
        artifact_root_host = self.resolve_host_path(artifact_root_container)
        if artifact_root_host:
            binds.append(f"{artifact_root_host}:{artifact_root_container}")

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

    def _socket_group_add(self, socket_path: str) -> list[str]:
        try:
            socket_gid = os.stat(socket_path).st_gid
        except OSError:
            return []
        return [str(socket_gid)]


class RancherLauncher(Launcher):
    def __init__(self):
        default_socket = _CHILD_SOCKET_PATH if os.path.exists(_CHILD_SOCKET_PATH) else os.path.expanduser("~/.rd/docker.sock")
        super().__init__(socket_path=os.environ.get("DOCKER_SOCKET", default_socket))


def get_launcher():
    runtime = (os.environ.get("CONTAINER_RUNTIME") or "docker").strip().lower()
    if runtime == "rancher":
        return RancherLauncher()
    return Launcher()