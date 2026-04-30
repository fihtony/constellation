"""Unified agent message bus.

Abstracts the difference between A2A HTTP calls and IM channel callbacks so
that any agent can send a message with a single ``AgentBus.send()`` call,
without knowing whether the recipient is another agent or an IM channel.

Destination naming convention:
    ``agent:<capability>``   — route to agent via Registry discovery
    ``callback:<url>``       — POST to a callback URL (Compass or Team Lead)
    ``im:<channel-id>``      — forward to IM Gateway (future)

Usage::

    bus = AgentBus()
    bus.send("callback:http://compass:8080/tasks/xyz/callbacks", "Done!")
    bus.send("agent:scm.pr.create", json.dumps(pr_payload))
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen

_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
_DEFAULT_TIMEOUT = 15


@dataclass
class Destination:
    type: str        # "agent" | "callback" | "im"
    address: str     # capability id, URL, or channel id


class AgentBus:
    """Unified outbound message interface."""

    def __init__(
        self,
        *,
        registry_url: str | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self._registry_url = registry_url or _REGISTRY_URL
        self._timeout = timeout

    def send(
        self,
        to: str,
        content: str,
        metadata: dict | None = None,
    ) -> dict:
        """Send *content* to *to*.

        Args:
            to: Destination string.  See module docstring for naming convention.
            content: Text content of the message.
            metadata: Optional metadata dict attached to the message.

        Returns:
            Response dict from the destination.

        Raises:
            ``ValueError`` if the destination string is malformed.
            ``URLError`` / ``OSError`` on network failure.
        """
        dest = self._resolve(to)
        if dest.type == "callback":
            return self._http_callback(dest.address, content, metadata)
        if dest.type == "agent":
            return self._a2a_send(dest.address, content, metadata)
        if dest.type == "im":
            return self._im_send(dest.address, content, metadata)
        raise ValueError(f"Unknown destination type: {dest.type!r}")

    def _resolve(self, to: str) -> Destination:
        if ":" not in to:
            raise ValueError(
                f"Destination must be prefixed with type, e.g. 'callback:<url>' or "
                f"'agent:<capability>'. Got: {to!r}"
            )
        prefix, _, address = to.partition(":")
        prefix = prefix.lower().strip()
        if prefix not in ("agent", "callback", "im"):
            raise ValueError(f"Unknown destination prefix: {prefix!r}")
        return Destination(type=prefix, address=address)

    def _http_callback(self, url: str, content: str, metadata: dict | None) -> dict:
        payload = {"message": content, **(metadata or {})}
        req = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read().decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def _a2a_send(self, capability: str, content: str, metadata: dict | None) -> dict:
        agent_url = self._discover_agent(capability)
        if not agent_url:
            raise URLError(f"No agent found for capability: {capability!r}")
        payload = {
            "jsonrpc": "2.0",
            "id": "bus-send",
            "method": "message:send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": content}],
                },
                "metadata": metadata or {},
            },
        }
        req = Request(
            f"{agent_url}/message:send",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _im_send(self, channel_id: str, content: str, metadata: dict | None) -> dict:
        """Forward a message to the IM Gateway for delivery to a specific channel.

        The channel_id is the connector identifier ('slack', 'teams', etc.).
        metadata must contain 'user_id' and 'workspace_id' for delivery targeting.
        """
        im_gateway_url = os.environ.get("IM_GATEWAY_URL", "http://im-gateway:8070")
        payload = {
            "channel": channel_id,
            "content": content,
            "metadata": metadata or {},
        }
        try:
            req = Request(
                f"{im_gateway_url}/api/notifications",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as err:
            print(f"[agent_bus] im_send to {channel_id!r} failed: {err}", flush=True)
            return {"status": "logged", "channel": channel_id}

    def _discover_agent(self, capability: str) -> str | None:
        try:
            req = Request(
                f"{self._registry_url}/query?capability={capability}",
                headers={"Accept": "application/json"},
            )
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            agents = data.get("agents") or []
            for agent in agents:
                instances = agent.get("instances") or []
                for inst in instances:
                    url = inst.get("url") or agent.get("baseUrl")
                    if url:
                        return url.rstrip("/")
            return None
        except Exception:  # noqa: BLE001
            return None
