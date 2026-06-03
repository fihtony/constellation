"""Shared helpers for orchestrator agents that spawn per-task children.

Both ``compass`` and ``team_lead`` need to launch on-demand agents (office,
web-dev, code-review, future android-dev / ios-dev) through the
:class:`framework.launcher.Launcher` machinery. The launch flow is the same
in both places:

1. Resolve a per-task launch definition from the Capability Registry
   (e.g. ``web-dev.task.execute``, ``office.document.summarize``).
2. Hand the definition to :meth:`Launcher.launch_instance` so a child
   container is created with the right image, port, mounts, and labels.
3. Poll the container's ``/health`` until it accepts A2A traffic.
4. Send the user's request via :func:`framework.a2a.client.dispatch_sync`.
5. Either keep the container alive for revision cycles
   (``preserve_instance=True``) or destroy it in the ``finally`` block
   (single-shot tasks like office).

This module centralises that pattern so the two orchestrators stay
consistent. The defense-in-depth that prevents on-demand agents from
ever receiving the docker socket lives in
:mod:`framework.launcher`; this module only handles the wire-up.
"""

from __future__ import annotations

import time
import urllib.request
from typing import Any

from framework.launcher import get_launcher


def wait_for_agent_ready(base_url: str, timeout: int = 30) -> None:
    """Poll ``{base_url}/health`` until the child agent responds.

    Raises :class:`TimeoutError` if the agent does not become ready
    within ``timeout`` seconds. The caller is expected to have just
    created the container via :meth:`Launcher.launch_instance` and to
    own its destruction (either in a ``finally`` block here or via
    :func:`destroy_launch_instance` from the caller's session cache).
    """
    deadline = time.time() + timeout
    health_url = f"{base_url.rstrip('/')}/health"
    last_error = "agent did not become ready"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=2):
                return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(0.5)
    raise TimeoutError(
        f"Timed out waiting for launched agent at {health_url}: {last_error}"
    )


def dispatch_via_launcher(
    definition: dict[str, Any],
    *,
    capability: str,
    launch_task_id: str,
    message_parts: list[dict[str, Any]],
    metadata: dict[str, Any],
    timeout: int,
    preserve_instance: bool = False,
    per_task_agent_task_id: str = "",
    launch_overrides: dict[str, Any] | None = None,
) -> dict:
    """Launch a per-task agent container and dispatch a single A2A request.

    Parameters
    ----------
    definition:
        The agent definition dict (typically fetched from the Capability
        Registry). Must include ``agent_id`` and ``launch_spec``.
    capability:
        The A2A capability string the child agent advertises.
    launch_task_id:
        Container-name suffix and docker label. Reusing the same value
        across revision rounds keeps the existing container alive.
    message_parts, metadata, timeout:
        Forwarded to :func:`framework.a2a.client.dispatch_sync`.
    preserve_instance:
        When ``True`` the container is left running and the returned
        dict is annotated with a ``_launch`` payload so the caller can
        cache the child URL and reuse the same container for follow-up
        dispatches. When ``False`` the container is destroyed in
        ``finally`` (single-shot tasks).
    per_task_agent_task_id:
        Echoed into the ``_launch`` payload so session caches keyed on
        a per-task handle (e.g. team_lead's review counter) can later
        tell two cached children apart.
    launch_overrides:
        Optional per-call overrides for
        :meth:`Launcher.launch_instance` (e.g. ``env``,
        ``extra_binds`` for office source mounts).

    Returns
    -------
    dict
        The raw result of :func:`dispatch_sync`. When
        ``preserve_instance=True`` the result is a copy with an extra
        ``_launch`` key containing ``agentId``, ``serviceUrl``,
        ``containerName``, and ``perTaskAgentTaskId``.
    """
    from framework.a2a.client import dispatch_sync

    launcher = get_launcher()
    launch = launcher.launch_instance(
        definition,
        launch_task_id or "per-task-agent",
        launch_overrides=launch_overrides or {},
    )
    agent_id = (
        str(
            definition.get("agent_id")
            or definition.get("agentId")
            or capability
        ).strip()
        or capability
    )

    try:
        wait_for_agent_ready(launch["service_url"])
        result = dispatch_sync(
            url=launch["service_url"],
            capability=capability,
            message_parts=message_parts,
            metadata=metadata,
            timeout=timeout,
        )
        if preserve_instance and isinstance(result, dict):
            result = dict(result)
            result["_launch"] = {
                "agentId": agent_id,
                "serviceUrl": launch["service_url"],
                "containerName": launch["container_name"],
                "perTaskAgentTaskId": per_task_agent_task_id,
            }
        return result
    finally:
        if not preserve_instance:
            try:
                launcher.destroy_instance(agent_id, launch["container_name"])
            except Exception:
                pass


def destroy_launch_instance(launch_info: dict[str, Any] | None) -> bool:
    """Destroy a previously-preserved per-task agent container.

    ``launch_info`` is the ``_launch`` dict returned by
    :func:`dispatch_via_launcher` with ``preserve_instance=True``.
    Returns ``True`` if a destroy call was issued, ``False`` if the
    input was empty or invalid. Swallows all exceptions — the caller
    is expected to be best-effort about cleanup.
    """
    if not isinstance(launch_info, dict):
        return False

    container_name = str(launch_info.get("containerName") or "").strip()
    if not container_name:
        return False

    agent_id = (
        str(launch_info.get("agentId") or "unknown-agent").strip()
        or "unknown-agent"
    )
    try:
        get_launcher().destroy_instance(agent_id, container_name)
    except Exception:
        return False
    return True
