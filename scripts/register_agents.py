"""One-time registry upsert script for Constellation agent definitions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from framework.config import load_agent_config
from framework.registry_client import RegistryClient

AGENTS_DIR = PROJECT_ROOT / "agents"


def _agent_declares_capabilities(agent_id: str) -> bool:
    cfg = load_agent_config(agent_id, project_root=PROJECT_ROOT)
    return bool(cfg.to_dict().get("capabilities"))


def _iter_agent_ids(selected_agents: list[str] | None = None) -> list[str]:
    if selected_agents:
        return sorted({agent_id.strip() for agent_id in selected_agents if agent_id.strip()})

    agent_ids: list[str] = []
    for child in sorted(AGENTS_DIR.iterdir()):
        if not child.is_dir():
            continue
        config_path = child / "config.yaml"
        if not config_path.is_file():
            continue
        agent_id = child.name.replace("_", "-")
        if not _agent_declares_capabilities(agent_id):
            continue
        agent_ids.append(agent_id)
    return agent_ids


def _card_url(agent_id: str, payload: dict) -> str:
    port = payload.get("port") or payload.get("launchSpec", {}).get("port") or 80
    return f"http://{agent_id}:{port}/.well-known/agent-card.json"


def _build_registration_payload(agent_id: str) -> dict:
    cfg = load_agent_config(agent_id, project_root=PROJECT_ROOT)
    data = cfg.to_dict()
    capabilities = list(data.get("capabilities") or [])
    if not capabilities:
        raise ValueError(f"Agent '{agent_id}' does not declare capabilities in config.yaml")

    payload = {
        "agentId": data.get("agent_id", agent_id),
        "version": str(data.get("version", "1.0.0")),
        "capabilities": capabilities,
        "executionMode": data.get("execution_mode", "per-task"),
        "displayName": data.get("name", agent_id),
        "description": data.get("description", ""),
        "registeredBy": "manual-script",
    }

    launch_spec = data.get("launch_spec") or data.get("launchSpec") or {}
    if launch_spec:
        payload["launchSpec"] = launch_spec

    if data.get("port"):
        payload["port"] = data.get("port")

    scaling_policy = data.get("scaling_policy") or data.get("scalingPolicy") or {}
    if scaling_policy:
        payload["scalingPolicy"] = scaling_policy

    payload["cardUrl"] = _card_url(payload["agentId"], payload)
    return payload


def _upsert_agents(client: RegistryClient, agent_ids: Iterable[str], *, dry_run: bool, registered_by: str) -> list[dict]:
    results: list[dict] = []
    for agent_id in agent_ids:
        payload = _build_registration_payload(agent_id)
        payload["registeredBy"] = registered_by
        existing = client.get_definition(payload["agentId"])
        action = "update" if existing else "create"
        if dry_run:
            results.append({"action": action, "agentId": payload["agentId"], "payload": payload})
            continue
        response = client.upsert_agent(payload)
        results.append({"action": action, "agentId": payload["agentId"], "response": response})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Register current Constellation agents into the registry.")
    parser.add_argument("--registry-url", default="", help="Override registry base URL.")
    parser.add_argument("--agent", action="append", dest="agents", help="Register only the given agent id. Repeatable.")
    parser.add_argument("--dry-run", action="store_true", help="Print the payloads without calling the registry.")
    parser.add_argument("--registered-by", default="manual-script", help="Audit label stored in the registry record.")
    args = parser.parse_args()

    client = RegistryClient(args.registry_url or RegistryClient.from_config().url)
    if not args.dry_run and not client.url:
        raise SystemExit("Registry URL is not configured. Set REGISTRY_URL or pass --registry-url.")

    results = _upsert_agents(
        client,
        _iter_agent_ids(args.agents),
        dry_run=args.dry_run,
        registered_by=args.registered_by,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
