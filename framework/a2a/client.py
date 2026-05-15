"""A2A client — dispatch tasks to other agents and poll for results.

Supports:
  - dispatch: send a message to an agent and get a task ID back
  - poll: repeatedly GET /tasks/{id} until terminal state
  - callback: handle POST callbacks from downstream agents
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from framework.a2a.protocol import Task, TaskState


class A2AClient:
    """Client for sending A2A messages to other agents."""

    def __init__(self, timeout: int = 10, poll_interval: int = 5) -> None:
        self._timeout = timeout
        self._poll_interval = poll_interval

    async def dispatch(
        self,
        url: str | None = None,
        capability: str | None = None,
        message: dict | None = None,
        *,
        registry_client: Any = None,
        callback_url: str | None = None,
        wait: bool = True,
        max_poll_seconds: int = 600,
    ) -> dict:
        """Send a task message to a target agent.

        Parameters
        ----------
        url : str
            Direct service URL.  If not provided, *capability* + *registry_client*
            are used to discover the URL.
        capability : str
            Skill ID to discover via registry (e.g. ``jira.ticket.fetch``).
        message : dict
            The message payload (will be wrapped in A2A envelope).
        registry_client :
            RegistryClient instance for capability lookup.
        callback_url : str
            Optional callback URL for async result notification.
        wait : bool
            If True, poll until the task reaches a terminal state.
        max_poll_seconds : int
            Maximum time to poll before giving up.

        Returns
        -------
        dict
            The final task dict from the agent.
        """
        # Resolve target URL
        target_url = url
        if not target_url and capability and registry_client:
            target_url = await self._discover(capability, registry_client)
        if not target_url:
            raise ValueError("Either url or (capability + registry_client) must be provided")

        # Build A2A message envelope
        envelope = self._build_envelope(message or {}, callback_url)

        # POST /message:send
        send_url = f"{target_url.rstrip('/')}/message:send"
        response = self._http_post(send_url, envelope)
        task_data = response.get("task", response)
        task_id = task_data.get("id", "")

        if not wait:
            return response

        # Poll until terminal state
        return await self._poll_until_done(target_url, task_id, max_poll_seconds)

    async def get_task(self, base_url: str, task_id: str) -> dict:
        """GET /tasks/{task_id} from the target agent."""
        url = f"{base_url.rstrip('/')}/tasks/{task_id}"
        return self._http_get(url)

    async def send_callback(self, callback_url: str, payload: dict) -> None:
        """POST a callback notification to the orchestrator."""
        self._http_post(callback_url, payload)

    async def send_ack(self, base_url: str, task_id: str) -> None:
        """POST /tasks/{task_id}/ack to acknowledge task completion."""
        url = f"{base_url.rstrip('/')}/tasks/{task_id}/ack"
        self._http_post(url, {})

    # -- Internal helpers ---------------------------------------------------

    async def _discover(self, capability: str, registry_client: Any) -> str:
        """Look up service URL via Registry capability."""
        instances = await registry_client.find_instances(capability)
        if not instances:
            raise ValueError(f"No agent found for capability: {capability}")
        return instances[0].get("serviceUrl", instances[0].get("service_url", ""))

    def _build_envelope(self, message: dict, callback_url: str | None) -> dict:
        """Wrap a message in the A2A send envelope."""
        import uuid
        metadata = message.pop("metadata", {})
        if callback_url:
            metadata["orchestratorCallbackUrl"] = callback_url

        parts = []
        if isinstance(message, dict):
            # If message has a "text" key, use it as the part
            text = message.pop("text", None)
            if text:
                parts = [{"text": text}]
            elif message:
                parts = [{"text": json.dumps(message, ensure_ascii=False)}]

        return {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "parts": parts,
                "metadata": metadata,
            },
            "configuration": {
                "returnImmediately": True,
            },
        }

    async def _poll_until_done(
        self, base_url: str, task_id: str, max_seconds: int,
    ) -> dict:
        """Poll GET /tasks/{id} until a terminal state is reached."""
        url = f"{base_url.rstrip('/')}/tasks/{task_id}"
        deadline = time.time() + max_seconds
        while time.time() < deadline:
            try:
                result = self._http_get(url)
                task_data = result.get("task", result)
                state = task_data.get("status", {}).get("state", "")
                if state in {
                    TaskState.COMPLETED.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELLED.value,
                    TaskState.INPUT_REQUIRED.value,
                }:
                    return result
            except Exception:
                pass  # transient error — retry
            time.sleep(self._poll_interval)
        raise TimeoutError(f"Task {task_id} did not complete within {max_seconds}s")

    def _http_post(self, url: str, payload: dict) -> dict:
        """Synchronous HTTP POST returning parsed JSON."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read())

    def _http_get(self, url: str) -> dict:
        """Synchronous HTTP GET returning parsed JSON."""
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Synchronous convenience helper for use inside ToolRegistry.execute_sync()
# ---------------------------------------------------------------------------

def dispatch_sync(
    url: str,
    capability: str,
    message_parts: list[dict],
    metadata: dict | None = None,
    *,
    timeout: int = 300,
    poll_interval: int = 2,
) -> dict:
    """Send an A2A task synchronously and block until it completes.

    Designed for use inside ``BaseTool.execute_sync()`` where async code
    cannot be awaited.  Uses ``urllib`` (stdlib) — no event loop required.

    Parameters
    ----------
    url:
        Base URL of the target agent (e.g. ``http://team-lead:8030``).
    capability:
        Skill ID to set in ``metadata.requestedCapability``.
    message_parts:
        List of message part dicts (e.g. ``[{"text": "..."}]``).
    metadata:
        Extra metadata fields merged with requestedCapability.
    timeout:
        Maximum seconds to wait for task completion.
    poll_interval:
        Seconds between polling GET /tasks/{id}.

    Returns
    -------
    dict
        The completed task dict (``task.artifacts`` contains results).
    """
    import uuid

    meta = {**(metadata or {}), "requestedCapability": capability}
    envelope = {
        "message": {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "parts": message_parts,
            "metadata": meta,
        },
        "configuration": {"returnImmediately": True},
    }

    data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/message:send",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        response = json.loads(resp.read())

    task_data = response.get("task", response)
    task_id = task_data.get("id", "")
    if not task_id:
        return response

    # Poll until terminal state
    poll_url = f"{url.rstrip('/')}/tasks/{task_id}"
    deadline = time.time() + timeout
    terminal = {
        "TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
        "TASK_STATE_CANCELLED", "TASK_STATE_INPUT_REQUIRED",
    }

    while time.time() < deadline:
        get_req = urllib.request.Request(poll_url, method="GET")
        try:
            with urllib.request.urlopen(get_req, timeout=30) as resp:
                result = json.loads(resp.read())
            state = result.get("task", result).get("status", {}).get("state", "")
            if state in terminal:
                return result
        except Exception:
            pass
        time.sleep(poll_interval)

    raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")

