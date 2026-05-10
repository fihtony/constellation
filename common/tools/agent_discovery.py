"""Helpers for discovering boundary agents through the Capability Registry."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen


def query_agents_by_capability(registry_url: str, capability: str) -> list[dict]:
    req = Request(
        f"{registry_url.rstrip('/')}/query?capability={capability}",
        headers={"Accept": "application/json"},
    )
    with urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.get("agents") or data.get("items") or [])
    return []


def extract_agent_url(agent: dict) -> str | None:
    for instance in agent.get("instances") or []:
        url = (
            instance.get("url")
            or instance.get("serviceUrl")
            or instance.get("service_url")
            or agent.get("baseUrl")
            or agent.get("serviceUrl")
            or agent.get("service_url")
        )
        if url:
            return str(url).rstrip("/")

    card_url = str(agent.get("card_url") or agent.get("cardUrl") or "").strip()
    if card_url.startswith("http://") or card_url.startswith("https://"):
        return card_url.rsplit("/.well-known/agent-card.json", 1)[0].rstrip("/")
    return None


def discover_capability_url(registry_url: str, capability: str) -> str | None:
    for agent in query_agents_by_capability(registry_url, capability):
        url = extract_agent_url(agent)
        if url:
            return url
    return None