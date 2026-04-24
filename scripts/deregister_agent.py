#!/usr/bin/env python3
"""Deregister an Agent Definition from the Capability Registry.

Marks the Agent Definition as 'deregistered'. Running instances can still
finish their current tasks but no new tasks will be routed.

Usage:
  python scripts/deregister_agent.py --agent-id echo-agent
"""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def deregister(registry_url, agent_id):
    req = Request(
        f"{registry_url}/agents/{agent_id}",
        method="DELETE",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"✓ Agent '{agent_id}' deregistered successfully.")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"✗ Deregistration failed. HTTP {e.code}: {body}", file=sys.stderr)
        return 1
    except URLError as e:
        print(f"✗ Cannot reach registry at {registry_url}: {e}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description="Deregister an Agent Definition")
    parser.add_argument("--registry-url", default="http://localhost:9000",
                        help="Capability Registry URL (default: http://localhost:9000)")
    parser.add_argument("--agent-id", required=True, help="Agent ID to deregister")
    args = parser.parse_args()

    sys.exit(deregister(args.registry_url, args.agent_id))


if __name__ == "__main__":
    main()
