"""Report persistent or per-task agent instances to the registry."""

from __future__ import annotations

import atexit
import json
import os
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_DISABLED_VALUES = {
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
            body = response.read().decode("utf-8")
            parsed = json.loads(body) if body else None
            return {
                "ok": True,
                "status": getattr(response, "status", 200),
                "body": parsed,
            }
    except HTTPError as error:
        print(f"[reporter] Failed to reach registry: {error}")
        return {
            "ok": False,
            "status": error.code,
            "body": None,
        }
    except (URLError, OSError) as error:
        print(f"[reporter] Failed to reach registry: {error}")
        return {
            "ok": False,
            "status": None,
            "body": None,
        }


class InstanceReporter:
    def __init__(
        self,
        agent_id,
        service_url,
        port,
        container_id=None,
        *,
        registry_url=None,
        heartbeat_interval=None,
        enabled=None,
    ):
        self.agent_id = agent_id
        self.service_url = service_url
        self.port = port
        self.container_id = container_id or os.environ.get("CONTAINER_ID") or f"{agent_id}-local"
        self.registry_url = (registry_url or os.environ.get("REGISTRY_URL", "http://registry:9000")).rstrip("/")
        raw_interval = (
            heartbeat_interval
            if heartbeat_interval is not None
            else os.environ.get("HEARTBEAT_INTERVAL", "30")
        )
        try:
            self.heartbeat_interval = max(1, int(raw_interval))
        except (TypeError, ValueError):
            self.heartbeat_interval = 30
        if enabled is None:
            raw_enabled = os.environ.get("INSTANCE_REPORTER_ENABLED", "1").strip().lower()
            self.enabled = raw_enabled not in _DISABLED_VALUES
        else:
            self.enabled = bool(enabled)
        self.instance_id = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not self.enabled:
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
        result = _post_json(
            f"{self.registry_url}/agents/{self.agent_id}/instances",
            {
                "serviceUrl": self.service_url,
                "port": self.port,
                "containerId": self.container_id,
            },
        )
        if result.get("ok"):
            return result.get("body") or {}
        return None

    def _remove(self):
        try:
            request = Request(
                f"{self.registry_url}/agents/{self.agent_id}/instances/{self.instance_id}",
                method="DELETE",
            )
            urlopen(request, timeout=3)
            print(f"[reporter] Removed instance {self.instance_id}")
        except (URLError, OSError):
            pass

    def _heartbeat_loop(self):
        while not self._stop.wait(self.heartbeat_interval):
            if self.instance_id:
                self._heartbeat_once()

    def _heartbeat_once(self):
        result = _post_json(
                    f"{self.registry_url}/agents/{self.agent_id}/instances/{self.instance_id}",
                    {"heartbeat": True},
                    method="PUT",
                )
        if result.get("ok"):
            return
        if result.get("status") != 404:
            return

        recovered = self._register()
        if recovered and recovered.get("instance_id"):
            self.instance_id = recovered["instance_id"]
            print(f"[reporter] Re-registered instance {self.instance_id} for {self.agent_id}")