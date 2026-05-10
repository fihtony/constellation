"""Launcher tool for per-task Constellation agents.

Provides a tool that wraps the launcher infrastructure, allowing the agentic
runtime to launch per-task agents on demand through Docker/Rancher.
"""

from __future__ import annotations

import json
import os
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
_LAUNCH_WAIT_TIMEOUT = int(os.environ.get("LAUNCH_WAIT_TIMEOUT", "120"))
_LAUNCH_POLL_INTERVAL = 2  # seconds


class LaunchPerTaskAgentTool(ConstellationTool):
    """Launch a per-task agent container and wait for it to register."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="launch_per_task_agent",
            description=(
                "Launch a per-task agent container via Docker/Rancher and wait for it to "
                "register with the Registry. Returns the service URL once the agent is ready. "
                "Use this when dispatch_agent_task fails because no idle instance is available "
                "for a per-task capability."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "capability": {
                        "type": "string",
                        "description": "Agent capability to launch, e.g. 'team-lead.task.analyze'",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task ID that triggered this launch (used for container naming).",
                    },
                    "extra_binds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional extra Docker bind mounts (e.g. for office file access).",
                    },
                },
                "required": ["capability", "task_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        capability = str(args.get("capability") or "").strip()
        task_id = str(args.get("task_id") or "").strip()
        extra_binds = list(args.get("extra_binds") or [])

        if not capability:
            return self.error("Missing required argument: capability")
        if not task_id:
            return self.error("Missing required argument: task_id")

        # Discover agent registration info from registry
        agent_info = self._lookup_agent_for_capability(capability)
        if not agent_info:
            return self.error(
                f"No agent registered for capability '{capability}'. "
                "Cannot launch — agent must be registered in the registry first."
            )

        if agent_info.get("execution_mode") != "per-task":
            return self.error(
                f"Agent '{agent_info.get('agent_id')}' is not a per-task agent. "
                "Only per-task agents can be launched dynamically."
            )

        # Apply extra binds if provided
        if extra_binds:
            launch_spec = dict(agent_info.get("launch_spec") or {})
            existing = list(launch_spec.get("extraBinds") or [])
            launch_spec["extraBinds"] = existing + extra_binds
            agent_info["launch_spec"] = launch_spec

        # Launch via the launcher
        try:
            from common.launcher import get_launcher
            launcher = get_launcher()
            launch_info = launcher.launch_instance(agent_info, task_id)
        except Exception as exc:
            return self.error(f"Failed to launch agent for '{capability}': {exc}")

        # Wait for the agent to register with the registry
        container_name = launch_info.get("container_name", "")
        agent_id = agent_info["agent_id"]
        instance = self._wait_for_registration(agent_id, container_name)

        if instance is None:
            return self.error(
                f"Agent '{agent_id}' was launched but did not register within "
                f"{_LAUNCH_WAIT_TIMEOUT}s. Container: {container_name}"
            )

        return self.ok(
            json.dumps(
                {
                    "agentId": agent_id,
                    "instanceId": instance["instance_id"],
                    "serviceUrl": instance["service_url"],
                    "containerName": container_name,
                    "capability": capability,
                },
                ensure_ascii=False,
            )
        )

    def _lookup_agent_for_capability(self, capability: str) -> dict | None:
        """Query registry for agent info including launch_spec."""
        try:
            req = Request(
                f"{_REGISTRY_URL}/query?capability={capability}",
                headers={"Accept": "application/json"},
            )
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                agents = data
            elif isinstance(data, dict):
                agents = data.get("agents") or data.get("items") or []
            else:
                agents = []
            if agents:
                return agents[0]
            return None
        except Exception:
            return None

    def _wait_for_registration(self, agent_id: str, container_name: str) -> dict | None:
        """Poll registry until an idle instance appears for the given agent."""
        deadline = time.time() + _LAUNCH_WAIT_TIMEOUT
        while time.time() < deadline:
            try:
                req = Request(
                    f"{_REGISTRY_URL}/agents/{agent_id}/instances",
                    headers={"Accept": "application/json"},
                )
                with urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    instances = data
                elif isinstance(data, dict):
                    instances = data.get("instances") or data.get("items") or []
                else:
                    instances = []
                matched_idle = None
                fallback_idle = None
                for inst in instances:
                    if inst.get("status") != "idle":
                        continue
                    if fallback_idle is None:
                        fallback_idle = inst
                    container_id = str(inst.get("container_id") or inst.get("containerId") or "").strip()
                    service_url = str(inst.get("service_url") or inst.get("serviceUrl") or "").strip()
                    if container_id == container_name or f"http://{container_name}:" in service_url:
                        matched_idle = inst
                        break
                if matched_idle is not None:
                    return matched_idle
                if fallback_idle is not None and len(instances) == 1:
                    return fallback_idle
            except (URLError, OSError):
                pass
            time.sleep(_LAUNCH_POLL_INTERVAL)
        return None


register_tool(LaunchPerTaskAgentTool())
