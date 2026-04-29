"""Shared runtime cache for active agents discovered from the capability registry."""

from __future__ import annotations

import os
import threading
import time

from common.registry_client import RegistryClient


class AgentDirectoryError(RuntimeError):
    """Base error raised by the runtime agent directory."""


class RegistryUnavailableError(AgentDirectoryError):
    """Raised when the registry cannot be queried."""


class CapabilityUnavailableError(AgentDirectoryError):
    """Raised when no running agent advertises a required capability."""


class AgentDirectory:
    def __init__(
        self,
        owner_agent_id: str,
        registry_client: RegistryClient | None = None,
        *,
        cache_ttl_seconds: int | None = None,
        watch_interval_seconds: int | None = None,
    ):
        self.owner_agent_id = owner_agent_id
        self.registry = registry_client or RegistryClient()
        self.cache_ttl_seconds = int(
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else os.environ.get("AGENT_DIRECTORY_CACHE_TTL_SECONDS", "30")
        )
        self.watch_interval_seconds = int(
            watch_interval_seconds
            if watch_interval_seconds is not None
            else os.environ.get("AGENT_DIRECTORY_WATCH_INTERVAL_SECONDS", "5")
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._agents: list[dict] = []
        self._capability_index: dict[str, list[dict]] = {}
        self._topology_version = 0
        self._topology_updated_at = 0.0
        self._last_refresh_at = 0.0

    def start(self):
        if self._thread is not None:
            return
        self.refresh(force=True)
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def topology_state(self) -> dict:
        with self._lock:
            return {
                "version": self._topology_version,
                "updatedAt": self._topology_updated_at,
                "lastRefreshAt": self._last_refresh_at,
            }

    def list_agents(self, *, force=False) -> list[dict]:
        self.refresh(force=force)
        with self._lock:
            return [agent.copy() for agent in self._agents]

    def find_capability(self, capability: str, *, refresh_on_miss=True) -> list[dict]:
        self.refresh(force=False)
        with self._lock:
            matches = list(self._capability_index.get(capability, []))
        if matches or not refresh_on_miss:
            return matches

        self.refresh(force=True)
        with self._lock:
            return list(self._capability_index.get(capability, []))

    def resolve_capability(self, capability: str, *, require_running_instance=True) -> tuple[dict, dict | None]:
        agents = self.find_capability(capability, refresh_on_miss=True)
        if not agents:
            raise CapabilityUnavailableError(
                f"No active agent advertises capability '{capability}'."
            )

        selected_agent = agents[0]
        selected_instance = None
        for agent in agents:
            instances = agent.get("instances", [])
            idle_instance = next(
                (instance for instance in instances if instance.get("status") == "idle"),
                None,
            )
            if idle_instance:
                return agent, idle_instance
            if selected_instance is None and instances:
                selected_agent = agent
                selected_instance = instances[0]

        if require_running_instance and selected_instance is None:
            raise CapabilityUnavailableError(
                f"Capability '{capability}' is registered but has no running instances."
            )
        return selected_agent, selected_instance

    def refresh(self, *, force=False) -> dict:
        with self._lock:
            stale = (time.time() - self._last_refresh_at) >= self.cache_ttl_seconds
            if not force and self._agents and not stale:
                return {
                    "version": self._topology_version,
                    "updatedAt": self._topology_updated_at,
                    "lastRefreshAt": self._last_refresh_at,
                }

        try:
            agents = self.registry.find_any_active()
            topology = self.registry.get_topology()
        except Exception as err:  # noqa: BLE001
            raise RegistryUnavailableError(str(err)) from err

        capability_index: dict[str, list[dict]] = {}
        cached_agents = []
        for agent in agents or []:
            normalized = dict(agent)
            normalized["instances"] = [dict(instance) for instance in agent.get("instances", [])]
            cached_agents.append(normalized)
            for capability in normalized.get("capabilities", []) or []:
                capability_index.setdefault(capability, []).append(normalized)

        with self._lock:
            self._agents = cached_agents
            self._capability_index = capability_index
            self._topology_version = int(topology.get("version", self._topology_version or 0))
            self._topology_updated_at = float(topology.get("updatedAt", time.time()))
            self._last_refresh_at = time.time()
            return {
                "version": self._topology_version,
                "updatedAt": self._topology_updated_at,
                "lastRefreshAt": self._last_refresh_at,
            }

    def _watch_loop(self):
        while not self._stop.wait(self.watch_interval_seconds):
            try:
                version = self.topology_state().get("version", 0)
                if not version:
                    self.refresh(force=True)
                    continue
                payload = self.registry.get_events(version)
                remote_version = int(payload.get("version", version))
                if remote_version != version:
                    self.refresh(force=True)
            except Exception as err:  # noqa: BLE001
                print(f"[{self.owner_agent_id}] Agent directory watcher warning: {err}")