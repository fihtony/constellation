#!/usr/bin/env python3
"""Resolve the effective agentic runtime backend for a Constellation agent.

Reads common/.env first, then <agent>/.env, applying the same override
hierarchy used at runtime. Prints the effective AGENT_RUNTIME value and
(optionally) which Dockerfile to use for that backend.

Usage:
    python3 scripts/resolve-agent-runtime.py <agent-name>
    python3 scripts/resolve-agent-runtime.py team-lead
    python3 scripts/resolve-agent-runtime.py android --dockerfile

Exit codes:
    0 — success
    1 — agent directory not found or configuration error
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.resolve()

_SUPPORTED_BACKENDS = {"copilot-cli", "connect-agent", "claude-code"}

# Agents that are non-agentic control-plane services (no backend Dockerfile)
_NON_AGENTIC_AGENTS = {"compass", "registry", "jira", "scm", "ui-design"}


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict, ignoring comments and blank lines."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
        if not m:
            continue
        key = m.group(1)
        value = m.group(2).strip().strip('"').strip("'")
        result[key] = value
    return result


def resolve_effective_backend(agent_name: str) -> tuple[str, str | None]:
    """Return (effective_backend, dockerfile_path | None).

    Reads common/.env then <agent>/.env and returns the AGENT_RUNTIME value
    that would be active at container startup.  Returns None for the
    dockerfile_path if the agent does not have a backend-specific Dockerfile
    (e.g. non-agentic services).
    """
    # 1. Shared defaults
    common_env = _parse_dotenv(_REPO_ROOT / "common" / ".env")

    # 2. Agent-local overrides
    agent_dir = _REPO_ROOT / agent_name
    if not agent_dir.is_dir():
        # Try hyphen → underscore mapping (e.g. team-lead → team_lead)
        alt = agent_name.replace("-", "_")
        agent_dir = _REPO_ROOT / alt
    if not agent_dir.is_dir():
        print(f"ERROR: Agent directory not found: {agent_name}", file=sys.stderr)
        sys.exit(1)

    agent_env = _parse_dotenv(agent_dir / ".env")

    # Merge: agent overrides common
    merged = {**common_env, **agent_env}

    # Also read from environment (highest priority)
    env_override = os.environ.get("AGENT_RUNTIME", "").strip()

    effective = env_override or merged.get("AGENT_RUNTIME", "copilot-cli")
    effective = effective.lower()

    if effective not in _SUPPORTED_BACKENDS:
        print(
            f"WARNING: Unrecognised AGENT_RUNTIME '{effective}'. "
            f"Supported: {', '.join(sorted(_SUPPORTED_BACKENDS))}",
            file=sys.stderr,
        )

    # Resolve Dockerfile path
    dockerfile_path: str | None = None
    if agent_name not in _NON_AGENTIC_AGENTS:
        candidates = [
            agent_dir / f"Dockerfile.{effective}",
            agent_dir / "Dockerfile",
        ]
        for candidate in candidates:
            if candidate.is_file():
                dockerfile_path = str(candidate)
                break

    return effective, dockerfile_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve effective agentic runtime backend for a Constellation agent."
    )
    parser.add_argument("agent", help="Agent name (e.g. team-lead, android, web)")
    parser.add_argument(
        "--dockerfile",
        action="store_true",
        help="Also print the resolved Dockerfile path.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output result as JSON.",
    )
    args = parser.parse_args()

    effective, dockerfile_path = resolve_effective_backend(args.agent)

    if args.as_json:
        import json
        print(
            json.dumps(
                {
                    "agent": args.agent,
                    "effectiveBackend": effective,
                    "dockerfilePath": dockerfile_path,
                },
                indent=2,
            )
        )
    elif args.dockerfile:
        print(f"backend={effective}")
        print(f"dockerfile={dockerfile_path or 'NOT_FOUND'}")
    else:
        print(effective)


if __name__ == "__main__":
    main()
