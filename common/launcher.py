"""Launch per-task agent containers through the Docker socket."""

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


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


class Launcher:
    def __init__(self):
        self.socket_path = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
        self.runtime_image = os.environ.get("DEFAULT_DYNAMIC_IMAGE", "constellation-android-agent:latest")
        self.runtime_network = os.environ.get("DYNAMIC_AGENT_NETWORK", "constellation-network")
        self.registry_url = os.environ.get("REGISTRY_URL", "http://registry:9000")
        self.default_port = int(os.environ.get("DYNAMIC_AGENT_PORT", "8000"))

    def _request_raw(self, method, path, payload=None):
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
        effective_launch_spec = dict(launch_spec)

        container_prefix = effective_launch_spec.get("namePrefix", agent_definition["agent_id"].replace("_", "-"))
        # Add a short unique suffix to prevent name collisions when the same
        # internal task_id is reused across successive per-task containers.
        unique_suffix = uuid.uuid4().hex[:8]
        container_name = f"{container_prefix}-{task_id.lower()}-{unique_suffix}"
        port = int(effective_launch_spec.get("port", self.default_port))
        service_url = f"http://{container_name}:{port}"
        image = effective_launch_spec.get("image", self.runtime_image)
        command = effective_launch_spec.get("command") or ["python3", "android/app.py"]

        env = {}
        env_file = effective_launch_spec.get("envFile")
        if env_file:
            env.update(load_dotenv(env_file))
        for key in effective_launch_spec.get("passThroughEnv", []):
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        # Inline env vars defined directly in launchSpec (e.g. per-agent model selection)
        for key, value in effective_launch_spec.get("env", {}).items():
            env[str(key)] = str(value)

        runtime_name = str(env.get("AGENT_RUNTIME") or os.environ.get("AGENT_RUNTIME") or "").strip()
        runtime_contribution = None
        if runtime_name:
            try:
                from common.runtime.provider_registry import get_launch_contribution

                runtime_contribution = get_launch_contribution(
                    runtime_name,
                    {
                        "agent_definition": agent_definition,
                        "launch_spec": effective_launch_spec,
                        "task_id": task_id,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[launcher] Failed to resolve runtime launch contribution for {runtime_name!r}: {exc}")

        if runtime_contribution:
            for key, value in (runtime_contribution.env or {}).items():
                env[str(key)] = str(value)
            if runtime_contribution.launcher_profile and not ((effective_launch_spec.get("security") or {}).get("launcherProfile")):
                security = dict(effective_launch_spec.get("security") or {})
                security["launcherProfile"] = runtime_contribution.launcher_profile
                effective_launch_spec["security"] = security

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
        if effective_launch_spec.get("mountDockerSocket", False) and os.path.exists(self.socket_path):
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

        # Apply security launcher profile from launchSpec.security.launcherProfile
        self._apply_launcher_profile(effective_launch_spec, payload)

        # Mount the artifacts volume so the launched agent can read/write workspace files.
        # The host path is discovered automatically by inspecting this container's mounts via
        # the Docker API; no separate ARTIFACT_ROOT_HOST variable is needed.
        artifact_root_container = os.environ.get("ARTIFACT_ROOT", "/app/artifacts")
        artifact_root_host = self._discover_host_source(artifact_root_container)
        binds = []
        if artifact_root_host:
            binds.append(f"{artifact_root_host}:{artifact_root_container}")
        # Only mount the Docker socket when explicitly requested in the launch spec
        # (mountDockerSocket: true).  Default is false so that execution agents such
        # as web-agent cannot reach the Docker daemon — principle of least privilege.
        if host_socket_path:
            binds.append(f"{host_socket_path}:{_CHILD_SOCKET_PATH}")
            group_add = self._socket_group_add(self.socket_path)
            if group_add:
                payload["HostConfig"]["GroupAdd"] = group_add
        for bind in effective_launch_spec.get("extraBinds", []) or []:
            bind_text = str(bind or "").strip()
            if bind_text:
                binds.append(bind_text)
        if runtime_contribution:
            for mount in runtime_contribution.mounts or []:
                source = str(getattr(mount, "source", "") or "").strip()
                target = str(getattr(mount, "target", "") or "").strip()
                if not source or not target:
                    continue
                suffix = ":ro" if getattr(mount, "read_only", True) else ""
                binds.append(f"{source}:{target}{suffix}")
        if binds:
            payload["HostConfig"]["Binds"] = binds

        self._request(
            "POST",
            f"/v1.43/containers/create?name={quote(container_name, safe='')}",
            payload=payload,
        )
        self._request("POST", f"/v1.43/containers/{quote(container_name, safe='')}/start")
        time.sleep(float(effective_launch_spec.get("startupDelaySeconds", 1.0)))
        return {
            "container_name": container_name,
            "service_url": service_url,
            "port": port,
        }

    def _discover_host_source(self, container_path):
        """Return the host-side source path for *container_path* by inspecting
        this container's volume mounts via the Docker API.

        When running on the host (not inside a Docker container) or when the
        API call fails, falls back to returning *container_path* directly so
        that bind-mounts still work in local development scenarios.
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

        Uses Docker API introspection to find the host-side mount source for
        ARTIFACT_ROOT, then maps *container_path* relative to it.
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

    # ------------------------------------------------------------------
    # Security profile helpers
    # ------------------------------------------------------------------

    # Supported launcher profiles and their Docker security constraints.
    _LAUNCHER_PROFILES = {
        "default": {},
        "docker-sandbox": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "CapAdd": ["NET_BIND_SERVICE"],
            "SecurityOpt": ["no-new-privileges"],
        },
        "docker-restricted": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "NetworkMode": "none",
        },
    }

    @classmethod
    def _apply_launcher_profile(cls, launch_spec: dict, payload: dict) -> None:
        """Apply security constraints from launchSpec.security.launcherProfile.

        Reads ``launch_spec["security"]["launcherProfile"]`` and merges the
        corresponding Docker HostConfig restrictions into *payload*.
        Unknown profiles are treated as ``"default"`` (no extra restrictions).
        """
        security = launch_spec.get("security") or {}
        requested_profile = str(security.get("launcherProfile") or "default").strip()
        profile = cls._LAUNCHER_PROFILES.get(requested_profile)
        applied_profile = requested_profile
        if profile is None:
            print(f"[launcher] Unknown launcher profile {requested_profile!r}, falling back to 'default'")
            profile = cls._LAUNCHER_PROFILES["default"]
            applied_profile = "default"

        host_config = payload.setdefault("HostConfig", {})
        for key, value in profile.items():
            host_config[key] = value

        # Record the requested/applied profiles in container labels for observability.
        labels = payload.setdefault("Labels", {})
        labels["constellation.requested_launcher_profile"] = requested_profile
        labels["constellation.launcher_profile"] = applied_profile


def get_launcher():
    """Return a launcher instance for the configured container runtime.

    Set ``CONTAINER_RUNTIME=rancher`` to use Rancher Desktop.
    Defaults to Docker Desktop when the variable is unset or set to ``docker``.
    """
    runtime = os.environ.get("CONTAINER_RUNTIME", "docker").strip().lower()
    if runtime == "rancher":
        from common.launcher_rancher import RancherLauncher
        return RancherLauncher()
    return Launcher()