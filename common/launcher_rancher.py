"""Launch per-task agent containers through the Rancher Desktop socket."""

from __future__ import annotations

import http.client
import json
import os
import socket
import time
import uuid
from urllib.parse import quote

from common.env_utils import load_dotenv


_CHILD_SOCKET_PATH = "/var/run/docker.sock"


def _is_containerized_process():
    return any((
        bool(os.environ.get("CONTAINER_ID", "").strip()),
        os.path.exists("/.dockerenv"),
        os.path.exists("/run/.containerenv"),
    ))


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


class RancherLauncher:
    """Launcher that targets Rancher Desktop's Docker-compatible API socket.

    Rancher Desktop exposes the same Docker Engine API as Docker Desktop, but
    the socket is located at a different path (``~/.rd/docker.sock`` on macOS).
    Set ``DOCKER_SOCKET`` to override if your Rancher installation differs.
    """

    def __init__(self):
        default_socket = _CHILD_SOCKET_PATH if _is_containerized_process() else os.path.expanduser("~/.rd/docker.sock")
        self.socket_path = os.environ.get("DOCKER_SOCKET", default_socket)
        self.runtime_image = os.environ.get("DEFAULT_DYNAMIC_IMAGE", "constellation-android-agent:latest")
        self.runtime_network = os.environ.get("DYNAMIC_AGENT_NETWORK", "constellation-network")
        self.registry_url = os.environ.get("REGISTRY_URL", "http://registry:9000")
        self.default_port = int(os.environ.get("DYNAMIC_AGENT_PORT", "8000"))

    def _request_raw(self, method, path, payload=None):
        if not os.path.exists(self.socket_path):
            raise RuntimeError(f"Rancher socket not available at {self.socket_path}")
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

    def _request(self, method, path, payload=None):
        status, raw = self._request_raw(method, path, payload=payload)

        if status >= 400:
            raise RuntimeError(f"Docker API {method} {path} failed: HTTP {status}: {raw}")
        if not raw:
            return None
        return json.loads(raw)

    def list_agent_containers(self, include_stopped=True):
        filters = quote(json.dumps({"label": ["constellation.agent_id"]}), safe="")
        path = f"/v1.43/containers/json?all={1 if include_stopped else 0}&filters={filters}"
        containers = self._request("GET", path) or []
        summarized = []
        for container in containers:
            labels = container.get("Labels") or {}
            names = container.get("Names") or []
            summarized.append({
                "container_id": container.get("Id", ""),
                "container_name": (names[0].lstrip("/") if names else labels.get("constellation.agent_id", "unknown-container")),
                "agent_id": labels.get("constellation.agent_id", "unknown-agent"),
                "display_name": labels.get("constellation.agent_name", labels.get("constellation.agent_id", "Unknown Agent")),
                "role": labels.get("constellation.agent_role", "unknown"),
                "state": container.get("State", "unknown"),
                "status": container.get("Status", "unknown"),
                "task_id": labels.get("constellation.task_id"),
            })
        return sorted(
            summarized,
            key=lambda item: (
                item["role"],
                item["display_name"],
                item["container_name"],
            ),
        )

    def read_container_logs(self, container_id, since=0, tail=200):
        if not container_id:
            return []
        safe_container_id = quote(container_id, safe="")
        path = (
            f"/v1.43/containers/{safe_container_id}/logs"
            f"?stdout=1&stderr=1&timestamps=1&tail={int(tail)}&since={max(0, int(since))}"
        )
        status, raw = self._request_raw("GET", path)
        if status >= 400:
            raise RuntimeError(f"Docker logs failed for {container_id}: HTTP {status}: {raw}")

        entries = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            if " " in line:
                timestamp, message = line.split(" ", 1)
            else:
                timestamp, message = "", line
            entries.append({"ts": timestamp, "line": message})
        return entries

    def launch_instance(self, agent_definition, task_id):
        launch_spec = agent_definition.get("launch_spec") or {}
        if not launch_spec:
            raise NotImplementedError(
                f"Agent '{agent_definition['agent_id']}' does not define a launchSpec for on-demand startup."
            )

        container_prefix = launch_spec.get("namePrefix", agent_definition["agent_id"].replace("_", "-"))
        unique_suffix = uuid.uuid4().hex[:8]
        container_name = f"{container_prefix}-{task_id.lower()}-{unique_suffix}"
        port = int(launch_spec.get("port", self.default_port))
        service_url = f"http://{container_name}:{port}"
        image = launch_spec.get("image", self.runtime_image)
        command = launch_spec.get("command") or ["python3", "android/app.py"]

        env = {}
        env_file = launch_spec.get("envFile")
        if env_file:
            env.update(load_dotenv(env_file))
        for key in launch_spec.get("passThroughEnv", []):
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        # Inline env vars defined directly in launchSpec (e.g. per-agent model selection)
        for key, value in launch_spec.get("env", {}).items():
            env[str(key)] = str(value)

        env.update({
            "HOST": "0.0.0.0",
            "PORT": str(port),
            "AGENT_ID": agent_definition["agent_id"],
            "REGISTRY_URL": self.registry_url,
            "ADVERTISED_BASE_URL": service_url,
            "CONTAINER_ID": container_name,
            "AUTO_STOP_AFTER_TASK": "1",
            "CONSTELLATION_TRUSTED_ENV": "1",
        })

        host_socket_path = ""
        if launch_spec.get("mountDockerSocket", False) and os.path.exists(self.socket_path):
            host_socket_path = self._discover_host_source(self.socket_path)
            env["DOCKER_SOCKET"] = _CHILD_SOCKET_PATH

        payload = {
            "Image": image,
            "Cmd": command,
            "Env": [f"{key}={value}" for key, value in sorted(env.items())],
            "Labels": {
                "constellation.agent_id": agent_definition["agent_id"],
                "constellation.agent_name": agent_definition.get("display_name", agent_definition["agent_id"]),
                "constellation.agent_role": agent_definition.get("execution_mode", "per-task"),
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
        platform = str(launch_spec.get("platform") or "").strip()
        if platform:
            payload["Platform"] = platform

        artifact_root_container = os.environ.get("ARTIFACT_ROOT", "/app/artifacts")
        artifact_root_host = self._discover_host_source(artifact_root_container)
        binds = []
        if artifact_root_host:
            binds.append(f"{artifact_root_host}:{artifact_root_container}")
        # Only mount the Docker socket when explicitly requested in the launch spec
        # (mountDockerSocket: true).  Default is false — principle of least privilege.
        if host_socket_path:
            binds.append(f"{host_socket_path}:{_CHILD_SOCKET_PATH}")
            group_add = self._socket_group_add(self.socket_path)
            if group_add:
                payload["HostConfig"]["GroupAdd"] = group_add
        for bind in launch_spec.get("extraBinds", []) or []:
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
        time.sleep(float(launch_spec.get("startupDelaySeconds", 1.0)))
        return {
            "container_name": container_name,
            "service_url": service_url,
            "port": port,
        }

    def _discover_host_source(self, container_path):
        """Return the host-side source path for *container_path* by inspecting
        this container's volume mounts via the Docker API.

        Falls back to *container_path* when not running inside a container or
        when the Docker API call fails.
        """
        container_id = (
            os.environ.get("CONTAINER_ID", "").strip()
            or os.environ.get("HOSTNAME", "").strip()
        )
        if not container_id:
            return container_path
        try:
            safe_id = quote(container_id, safe="")
            status, raw = self._request_raw("GET", f"/v1.43/containers/{safe_id}/json")
            if status >= 400 or not raw:
                return container_path
            data = json.loads(raw)
            target_real = os.path.realpath(container_path)
            for mount in data.get("Mounts", []):
                dest = mount.get("Destination", "")
                if dest and os.path.realpath(dest) == target_real:
                    src = mount.get("Source", "")
                    if src:
                        return src
        except Exception:
            pass
        return container_path

    def _socket_group_add(self, socket_path):
        try:
            socket_gid = os.stat(socket_path).st_gid
        except OSError:
            return []
        return [str(socket_gid)]

    def resolve_host_path(self, container_path):
        """Convert a container-side absolute path to its host-side equivalent.

        Returns an empty string if the path cannot be mapped.
        """
        if not container_path:
            return ""
        artifact_root_container = os.environ.get("ARTIFACT_ROOT", "/app/artifacts")
        artifact_root_host = self._discover_host_source(artifact_root_container)
        if not artifact_root_host:
            return ""
        container_real = os.path.realpath(container_path)
        base_real = os.path.realpath(artifact_root_container)
        try:
            relative = os.path.relpath(container_real, base_real)
            if relative.startswith(".."):
                return ""
        except ValueError:
            return ""
        return os.path.realpath(os.path.join(artifact_root_host, relative))

    def destroy_instance(self, agent_id, container_name):
        self._request("DELETE", f"/v1.43/containers/{quote(container_name, safe='')}?force=1")
