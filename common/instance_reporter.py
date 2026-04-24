"""Report persistent or per-task agent instances to the registry."""

from __future__ import annotations

import atexit
import json
import os
import threading
from urllib.error import URLError
from urllib.request import Request, urlopen

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000").rstrip("/")
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
INSTANCE_REPORTER_ENABLED = os.environ.get("INSTANCE_REPORTER_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def _post_json(url, payload, method="POST"):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method=method,
    )
    try:
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except (URLError, OSError) as error:
        print(f"[reporter] Failed to reach registry: {error}")
        return None


class InstanceReporter:
    def __init__(self, agent_id, service_url, port, container_id=None):
        self.agent_id = agent_id
        self.service_url = service_url
        self.port = port
        self.container_id = container_id or os.environ.get("CONTAINER_ID") or f"{agent_id}-local"
        self.instance_id = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not INSTANCE_REPORTER_ENABLED:
            print(f"[reporter] Instance reporting disabled for {self.agent_id}")
            return
        result = self._register()
        if result:
            self.instance_id = result.get("instance_id")
            print(f"[reporter] Registered instance {self.instance_id} for {self.agent_id}")
        else:
            print(f"[reporter] Warning: could not register instance for {self.agent_id}")
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self):
        self._stop.set()
        if self.instance_id:
            self._remove()

    def _register(self):
        return _post_json(
            f"{REGISTRY_URL}/agents/{self.agent_id}/instances",
            {
                "serviceUrl": self.service_url,
                "port": self.port,
                "containerId": self.container_id,
            },
        )

    def _remove(self):
        try:
            request = Request(
                f"{REGISTRY_URL}/agents/{self.agent_id}/instances/{self.instance_id}",
                method="DELETE",
            )
            urlopen(request, timeout=3)
            print(f"[reporter] Removed instance {self.instance_id}")
        except (URLError, OSError):
            pass

    def _heartbeat_loop(self):
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            if self.instance_id:
                _post_json(
                    f"{REGISTRY_URL}/agents/{self.agent_id}/instances/{self.instance_id}",
                    {"heartbeat": True},
                    method="PUT",
                )