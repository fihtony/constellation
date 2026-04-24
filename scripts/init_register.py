#!/usr/bin/env python3
"""Initialize the Capability Registry with all Agent Definitions.

Runs as a one-shot init service in docker-compose.
Waits for registry to be healthy, then auto-discovers all agents by scanning
each agent folder for a registry-config.json + agent-card.json pair and
registers them — no hard-coded definitions.

Discovery path (relative to /app):
  <agent>/registry-config.json  — registry-specific config (agentId, cardUrl,
                                   executionMode, scalingPolicy, launchSpec)
  <agent>/agent-card.json       — A2A agent card (displayName, description,
                                   version, skills)
"""

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
MAX_WAIT = 30

# Root directory where agent sub-folders live (inside the container: /app)
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def discover_agent_definitions():
    """
    Walk APP_ROOT looking for folders that contain both
    registry-config.json and agent-card.json.
    Returns a list of merged definition dicts ready to POST to the registry.
    """
    definitions = []
    try:
        entries = sorted(os.listdir(APP_ROOT))
    except OSError as exc:
        print(f"[init] ERROR: cannot list {APP_ROOT}: {exc}", file=sys.stderr)
        return definitions

    for entry in entries:
        folder = os.path.join(APP_ROOT, entry)
        if not os.path.isdir(folder):
            continue
        reg_cfg_path  = os.path.join(folder, "registry-config.json")
        card_path     = os.path.join(folder, "agent-card.json")
        if not os.path.isfile(reg_cfg_path):
            continue  # not an agent folder

        # Load registry config (required)
        with open(reg_cfg_path, encoding="utf-8") as fh:
            reg_cfg = json.load(fh)

        # Load agent card for display metadata (optional but expected)
        card = {}
        if os.path.isfile(card_path):
            with open(card_path, encoding="utf-8") as fh:
                card = json.load(fh)

        agent_id    = reg_cfg.get("agentId") or entry
        display_name = card.get("name") or agent_id
        description  = card.get("description") or ""
        version      = card.get("version") or reg_cfg.get("version") or "1.0.0"
        capabilities = [s["id"] for s in card.get("skills", [])]

        definition = {
            "agentId":       agent_id,
            "version":       version,
            "displayName":   display_name,
            "description":   description,
            "cardUrl":       reg_cfg.get("cardUrl", ""),
            "capabilities":  capabilities,
            "executionMode": reg_cfg.get("executionMode", "persistent"),
            "scalingPolicy": reg_cfg.get("scalingPolicy", {
                "maxInstances": 1,
                "perInstanceConcurrency": 1,
                "idleTimeoutSeconds": 300,
            }),
            "registeredBy": "init-script",
        }
        if "launchSpec" in reg_cfg:
            definition["launchSpec"] = reg_cfg["launchSpec"]

        definitions.append(definition)
        print(f"[init] Discovered: {agent_id} ({display_name}) in {folder}")

    return definitions


def wait_for_registry():
    """Wait until registry /health returns 200."""
    print("[init] Waiting for registry to be ready...")
    start = time.time()
    while time.time() - start < MAX_WAIT:
        try:
            with urlopen(f"{REGISTRY_URL}/health", timeout=2) as resp:
                if resp.status == 200:
                    print("[init] Registry is ready.")
                    return True
        except (URLError, OSError):
            pass
        time.sleep(1)
    print(f"[init] ERROR: Registry not ready after {MAX_WAIT}s", file=sys.stderr)
    return False


def register_agent(definition):
    """Register a single agent definition."""
    agent_id = definition["agentId"]
    data = json.dumps(definition, ensure_ascii=False).encode("utf-8")
    req = Request(
        f"{REGISTRY_URL}/agents",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"[init] ✓ Registered: {agent_id} (status={result.get('status', '?')})")
            return True
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[init] ✗ Failed to register {agent_id}: HTTP {e.code} — {body}", file=sys.stderr)
        return False
    except URLError as e:
        print(f"[init] ✗ Failed to register {agent_id}: {e}", file=sys.stderr)
        return False


def main():
    if not wait_for_registry():
        sys.exit(1)

    definitions = discover_agent_definitions()
    if not definitions:
        print("[init] WARNING: No agent definitions discovered — check APP_ROOT contains agent folders.", file=sys.stderr)
        sys.exit(1)

    print(f"[init] Registering {len(definitions)} agent(s)...")
    failed = 0
    for defn in definitions:
        if not register_agent(defn):
            failed += 1

    if failed:
        print(f"[init] WARNING: {failed} registration(s) failed.", file=sys.stderr)
        sys.exit(1)

    print(f"[init] All {len(definitions)} agent definition(s) registered. Done.")


if __name__ == "__main__":
    main()
