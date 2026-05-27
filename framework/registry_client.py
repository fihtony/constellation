"""V2 Registry client abstraction.

Provides a clean interface for agent discovery and capability lookup
without requiring callers to hard-code HTTP endpoints or fall through
multiple env-var / config / default layers manually.

Usage::

    from framework.registry_client import RegistryClient

    client = RegistryClient.from_config()
    url = client.discover("jira.ticket.fetch")
    # url == "http://jira:8080" or "" if not found
"""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)


def _persistent_service_url(definition: dict[str, Any]) -> str:
    execution_mode = str(
        definition.get("execution_mode") or definition.get("executionMode") or ""
    ).strip().lower()
    if execution_mode == "per-task":
        return ""

    card_url = str(definition.get("card_url") or definition.get("cardUrl") or "").strip()
    if not card_url:
        return ""

    parts = urlsplit(card_url)
    if not parts.scheme or not parts.netloc:
        return ""

    suffix = "/.well-known/agent-card.json"
    path = parts.path or ""
    if path.endswith(suffix):
        path = path[: -len(suffix)]
    else:
        path = path.rsplit("/", 1)[0] if "/" in path else ""
    path = path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


@dataclass
class ServiceInstance:
    """A discovered service instance."""

    agent_id: str
    service_url: str
    capabilities: list[str] = field(default_factory=list)
    status: str = "idle"


class RegistryClient:
    """V2 Registry client with built-in caching and config-based initialisation.

    Features:
    - Capability-based discovery via ``discover(capability)``
    - Agent lookup via ``lookup(agent_id)``
    - In-memory cache with configurable TTL
    - Transparent fallback to env / config / hardcoded defaults
    - Thread-safe
    """

    def __init__(self, registry_url: str, cache_ttl_seconds: int = 30):
        self._registry_url = registry_url.rstrip("/") if registry_url else ""
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, str]] = {}  # capability → (expiry, url)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, cache_ttl: int = 30) -> "RegistryClient":
        """Build a RegistryClient from environment / config.

        Resolution order:
        1. ``CONSTELLATION_REGISTRY_URL`` env var
        2. ``REGISTRY_URL`` env var
        3. ``config/constellation.yaml`` → ``registry.url``
        4. Empty string (discovery will return empty)
        """
        url = (
            os.environ.get("CONSTELLATION_REGISTRY_URL")
            or os.environ.get("REGISTRY_URL")
            or ""
        )
        if not url:
            try:
                from framework.config import load_global_config
                cfg = load_global_config()
                url = cfg.get("registry.url", "")
            except Exception:
                pass
        return cls(url, cache_ttl)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The base URL of the registry (may be empty if unconfigured)."""
        return self._registry_url

    def discover(self, capability: str) -> str:
        """Return the service URL of the first healthy instance for *capability*.

        Returns an empty string when the registry is unreachable, has no
        matching instance, or is unconfigured.
        """
        if not self._registry_url:
            return ""

        # Check cache
        with self._lock:
            cached = self._cache.get(capability)
            if cached and time.time() < cached[0]:
                return cached[1]

        try:
            definitions = self.query_capability(capability)
            for definition in definitions:
                nested_instances = definition.get("instances")
                if isinstance(nested_instances, list):
                    for instance in nested_instances:
                        svc_url = instance.get("serviceUrl") or instance.get("service_url") or ""
                        if svc_url:
                            with self._lock:
                                self._cache[capability] = (time.time() + self._cache_ttl, svc_url)
                            return svc_url

                svc_url = definition.get("serviceUrl") or definition.get("service_url") or ""
                if svc_url:
                    with self._lock:
                        self._cache[capability] = (time.time() + self._cache_ttl, svc_url)
                    return svc_url

            for definition in definitions:
                svc_url = _persistent_service_url(definition)
                if svc_url:
                    with self._lock:
                        self._cache[capability] = (time.time() + self._cache_ttl, svc_url)
                    return svc_url
        except Exception as exc:
            logger.debug("[registry-client] Discovery failed for %s: %s", capability, exc)

        return ""

    def lookup(self, agent_id: str) -> str:
        """Return the service URL for a specific agent_id.

        Returns an empty string when the registry is unreachable or
        has no matching agent.
        """
        if not self._registry_url:
            return ""

        try:
            body = self._request_json("GET", f"/agents/{quote(agent_id, safe='')}/instances")
            instances = body if isinstance(body, list) else body.get("instances", [])
            for instance in instances:
                svc_url = instance.get("serviceUrl") or instance.get("service_url") or ""
                if svc_url:
                    return svc_url
        except Exception as exc:
            logger.debug("[registry-client] Lookup failed for %s: %s", agent_id, exc)
            return ""

    def get_definition(self, agent_id: str) -> dict:
        """Return the registered definition for *agent_id* or an empty dict."""
        if not self._registry_url:
            return {}
        try:
            body = self._request_json("GET", f"/agents/{quote(agent_id, safe='')}")
            return body if isinstance(body, dict) else {}
        except Exception as exc:
            logger.debug("[registry-client] Definition lookup failed for %s: %s", agent_id, exc)
            return {}

    def query_capability(self, capability: str) -> list[dict]:
        """Return registry definitions advertising *capability*."""
        if not self._registry_url:
            return []
        try:
            body = self._request_json("GET", f"/query?capability={quote(capability, safe='')}")
            return body if isinstance(body, list) else body.get("instances", [])
        except Exception as exc:
            logger.debug("[registry-client] Capability query failed for %s: %s", capability, exc)
            return []

    def get_capability_definition(self, capability: str) -> dict:
        """Return the first definition that advertises *capability*."""
        definitions = self.query_capability(capability)
        return definitions[0] if definitions else {}

    def has_capability(self, capability: str) -> bool:
        """Return whether any active definition advertises *capability*."""
        return bool(self.query_capability(capability))

    def find_instances(self, capability: str) -> list[dict]:
        """Return flattened live instances for *capability*."""
        definitions = self.query_capability(capability)
        instances: list[dict] = []
        for definition in definitions:
            nested_instances = definition.get("instances")
            if isinstance(nested_instances, list) and nested_instances:
                for instance in nested_instances:
                    merged = dict(instance)
                    merged.setdefault("agentId", definition.get("agent_id") or definition.get("agentId", ""))
                    merged.setdefault("capabilities", definition.get("capabilities") or [])
                    instances.append(merged)
                continue

            svc_url = definition.get("serviceUrl") or definition.get("service_url") or ""
            if svc_url:
                merged = dict(definition)
                merged.setdefault("agentId", definition.get("agent_id") or definition.get("agentId", ""))
                instances.append(merged)
        return instances

    def upsert_agent(self, payload: dict[str, Any]) -> dict:
        """Create or replace an agent definition in the registry."""
        return self._request_json("POST", "/agents", payload=payload)

    def register_instance(
        self,
        agent_id: str,
        *,
        service_url: str,
        port: int,
        container_id: str = "",
    ) -> dict:
        """Register or refresh a live instance for *agent_id*."""
        payload = {
            "serviceUrl": service_url,
            "port": int(port or 0),
        }
        if container_id:
            payload["containerId"] = container_id
        return self._request_json(
            "POST",
            f"/agents/{quote(agent_id, safe='')}/instances",
            payload=payload,
        )

    def heartbeat_instance(self, agent_id: str, instance_id: str) -> dict:
        """Send a heartbeat update for an existing instance."""
        return self._request_json(
            "PUT",
            f"/agents/{quote(agent_id, safe='')}/instances/{quote(instance_id, safe='')}",
            payload={"heartbeat": True},
        )

    def invalidate(self, capability: str | None = None) -> None:
        """Invalidate the cache for a single capability (or all)."""
        with self._lock:
            if capability:
                self._cache.pop(capability, None)
            else:
                self._cache.clear()

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 3) -> Any:
        import urllib.request

        if not self._registry_url:
            raise RuntimeError("Registry URL is not configured")

        request = urllib.request.Request(
            f"{self._registry_url}{path}",
            data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"} if payload is not None else {},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def __repr__(self) -> str:
        return f"RegistryClient(url={self._registry_url!r})"
