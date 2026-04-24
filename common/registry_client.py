"""HTTP client for the Capability Registry."""

from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000").rstrip("/")
TIMEOUT = int(os.environ.get("REGISTRY_TIMEOUT", "5"))


def _fetch(url, method="GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


class RegistryClient:
    def __init__(self, base_url=None):
        self.base_url = (base_url or REGISTRY_URL).rstrip("/")

    def find_by_capability(self, capability):
        return _fetch(f"{self.base_url}/query?capability={capability}")

    def find_any_active(self):
        return _fetch(f"{self.base_url}/query")

    def get_definition(self, agent_id):
        try:
            return _fetch(f"{self.base_url}/agents/{agent_id}")
        except URLError:
            return None

    def list_instances(self, agent_id):
        return _fetch(f"{self.base_url}/agents/{agent_id}/instances")

    def mark_instance_busy(self, agent_id, instance_id, task_id):
        return _fetch(
            f"{self.base_url}/agents/{agent_id}/instances/{instance_id}",
            method="PUT",
            payload={"status": "busy", "current_task_id": task_id},
        )

    def mark_instance_idle(self, agent_id, instance_id):
        return _fetch(
            f"{self.base_url}/agents/{agent_id}/instances/{instance_id}",
            method="PUT",
            payload={"status": "idle", "current_task_id": None},
        )