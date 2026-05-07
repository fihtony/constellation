"""Helpers for locating the current orchestrator service for a task."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

ORCHESTRATOR_PROGRESS_CAPABILITY = "orchestrator.progress.report"


def derive_service_base_url(url: str | None) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def resolve_orchestrator_base_url(
    metadata: Mapping[str, object] | None,
    *,
    agent_directory=None,
    capability: str = ORCHESTRATOR_PROGRESS_CAPABILITY,
) -> str:
    payload = metadata if isinstance(metadata, Mapping) else {}

    callback_url = str(payload.get("orchestratorCallbackUrl") or "")
    callback_base_url = derive_service_base_url(callback_url)
    if callback_base_url:
        return callback_base_url

    if agent_directory is not None and capability:
        try:
            _, instance = agent_directory.resolve_capability(capability)
        except Exception:
            instance = None
        discovered_url = derive_service_base_url(str((instance or {}).get("service_url") or ""))
        if discovered_url:
            return discovered_url

    legacy_url = str(payload.get("orchestratorUrl") or "")
    return derive_service_base_url(legacy_url)