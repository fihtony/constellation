#!/usr/bin/env python3
"""Register an Agent Definition with the Capability Registry.

Usage:
  python scripts/register_agent.py --agent-id echo-agent \
    --card-url http://echo-agent:8000/.well-known/agent-card.json \
    --capabilities echo.process \
    --execution-mode persistent

  python scripts/register_agent.py --agent-id android-implement \
    --card-url http://android-agent:8000/.well-known/agent-card.json \
    --capabilities android.ui.implement,android.test.run \
    --execution-mode per-task
"""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def register(registry_url, agent_id, version, card_url, capabilities,
             execution_mode, scaling_policy, registered_by):
    payload = {
        "agentId": agent_id,
        "version": version,
        "cardUrl": card_url,
        "capabilities": capabilities,
        "executionMode": execution_mode,
        "scalingPolicy": scaling_policy,
        "registeredBy": registered_by,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        f"{registry_url}/agents",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"✓ Agent '{agent_id}' registered successfully.")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"✗ Registration failed. HTTP {e.code}: {body}", file=sys.stderr)
        return 1
    except URLError as e:
        print(f"✗ Cannot reach registry at {registry_url}: {e}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description="Register an Agent Definition")
    parser.add_argument("--registry-url", default="http://localhost:9000",
                        help="Capability Registry URL (default: http://localhost:9000)")
    parser.add_argument("--agent-id", required=True, help="Unique agent identifier")
    parser.add_argument("--version", default="1.0.0", help="Agent version")
    parser.add_argument("--card-url", required=True, help="Agent Card URL")
    parser.add_argument("--capabilities", required=True,
                        help="Comma-separated capability tags (e.g. echo.process,echo.test)")
    parser.add_argument("--execution-mode", default="persistent",
                        choices=["persistent", "per-task"],
                        help="Execution mode (default: persistent)")
    parser.add_argument("--max-instances", type=int, default=5)
    parser.add_argument("--idle-timeout", type=int, default=300)
    parser.add_argument("--registered-by", default="script")
    args = parser.parse_args()

    caps = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    scaling = {
        "maxInstances": args.max_instances,
        "perInstanceConcurrency": 1,
        "idleTimeoutSeconds": args.idle_timeout,
    }
    sys.exit(register(
        args.registry_url, args.agent_id, args.version,
        args.card_url, caps, args.execution_mode, scaling,
        args.registered_by,
    ))


if __name__ == "__main__":
    main()
