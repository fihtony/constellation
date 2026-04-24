"""Launch per-task agent containers through the Docker socket."""

from __future__ import annotations

import http.client
import json
import os
import socket
import time
from urllib.parse import quote

from common.env_utils import load_dotenv


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
        self.runtime_image = os.environ.get("DEFAULT_DYNAMIC_IMAGE", "mvp-android-agent:latest")
        self.runtime_network = os.environ.get("DYNAMIC_AGENT_NETWORK", "mvp-network")
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
        filters = quote(json.dumps({"label": ["mvp.agent_id"]}), safe="")
        path = f"/v1.43/containers/json?all={1 if include_stopped else 0}&filters={filters}"
        containers = self._request("GET", path) or []
        summarized = []
        for container in containers:
            labels = container.get("Labels") or {}
            names = container.get("Names") or []
            summarized.append({
                "container_id": container.get("Id", ""),
                "container_name": (names[0].lstrip("/") if names else labels.get("mvp.agent_id", "unknown-container")),
                "agent_id": labels.get("mvp.agent_id", "unknown-agent"),
                "display_name": labels.get("mvp.agent_name", labels.get("mvp.agent_id", "Unknown Agent")),
                "role": labels.get("mvp.agent_role", "unknown"),
                "state": container.get("State", "unknown"),
                "status": container.get("Status", "unknown"),
                "task_id": labels.get("mvp.task_id"),
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
        container_name = f"{container_prefix}-{task_id.lower()}"
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

        env.update({
            "HOST": "0.0.0.0",
            "PORT": str(port),
            "AGENT_ID": agent_definition["agent_id"],
            "REGISTRY_URL": self.registry_url,
            "ADVERTISED_BASE_URL": service_url,
            "CONTAINER_ID": container_name,
            "AUTO_STOP_AFTER_TASK": "1",
        })

        payload = {
            "Image": image,
            "Cmd": command,
            "Env": [f"{key}={value}" for key, value in sorted(env.items())],
            "Labels": {
                "mvp.agent_id": agent_definition["agent_id"],
                "mvp.agent_name": agent_definition.get("display_name", agent_definition["agent_id"]),
                "mvp.agent_role": agent_definition.get("execution_mode", "per-task"),
                "mvp.task_id": task_id,
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

        # Mount the artifacts volume so the android agent can write workspace files
        artifact_root_host = os.environ.get("ARTIFACT_ROOT_HOST", "").strip()
        artifact_root_container = os.environ.get("ARTIFACT_ROOT", "/app/artifacts")
        if artifact_root_host:
            payload["HostConfig"]["Binds"] = [
                f"{artifact_root_host}:{artifact_root_container}"
            ]

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

    def destroy_instance(self, agent_id, container_name):
        self._request("DELETE", f"/v1.43/containers/{quote(container_name, safe='')}?force=1")