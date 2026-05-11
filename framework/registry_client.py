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

logger = logging.getLogger(__name__)


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

        # Query registry
        try:
            import urllib.request

            url = f"{self._registry_url}/query?capability={capability}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            instances = body if isinstance(body, list) else body.get("instances", [])
            for inst in instances:
                svc_url = inst.get("serviceUrl") or inst.get("service_url") or ""
                if svc_url:
                    with self._lock:
                        self._cache[capability] = (
                            time.time() + self._cache_ttl,
                            svc_url,
                        )
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
            import urllib.request

            url = f"{self._registry_url}/agents/{agent_id}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body.get("serviceUrl") or body.get("service_url") or ""
        except Exception as exc:
            logger.debug("[registry-client] Lookup failed for %s: %s", agent_id, exc)
            return ""

    def invalidate(self, capability: str | None = None) -> None:
        """Invalidate the cache for a single capability (or all)."""
        with self._lock:
            if capability:
                self._cache.pop(capability, None)
            else:
                self._cache.clear()

    def __repr__(self) -> str:
        return f"RegistryClient(url={self._registry_url!r})"
