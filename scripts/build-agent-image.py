#!/usr/bin/env python3
"""Build a Constellation agent image using the backend-aware Dockerfile.

Reads common/.env + <agent>/.env to resolve the effective AGENT_RUNTIME,
selects the matching Dockerfile.<backend>, and runs 'docker build' with the
resolved configuration.

Usage:
    python3 scripts/build-agent-image.py <agent-name>
    python3 scripts/build-agent-image.py team-lead
    python3 scripts/build-agent-image.py android
    python3 scripts/build-agent-image.py --all

Options:
    --dry-run    Print the docker build command instead of running it.
    --all        Build all agents that have backend-specific Dockerfiles.
    --platform   Override platform (e.g. linux/amd64). Default: auto-detect from .env / OS.

Exit codes:
    0 — all requested builds succeeded
    1 — one or more builds failed or were misconfigured
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.resolve()
_SCRIPTS_DIR = Path(__file__).parent.resolve()
_RESOLVE_SCRIPT = str(_SCRIPTS_DIR / "resolve-agent-runtime.py")

# All agents that have per-backend Dockerfiles (Dockerfile.<backend>)
# 'registry' is excluded — it is a pure infrastructure service with no agentic runtime.
_AGENTIC_AGENTS = ["team-lead", "web", "android", "office", "compass", "jira", "scm", "ui-design"]

# Platform overrides for specific agents (architecture-sensitive toolchains)
_AGENT_PLATFORM_OVERRIDES: dict[str, str] = {
    "android": "linux/amd64",  # Android SDK binaries are x86_64-only
}


def _image_name(agent_name: str) -> str:
    return f"constellation-{agent_name}-agent:latest"


def _resolve_backend(agent_name: str) -> tuple[str, str | None]:
    """Return (effective_backend, dockerfile_path)."""
    sys.path.insert(0, str(_SCRIPTS_DIR))
    try:
        from resolve_agent_runtime import resolve_effective_backend  # type: ignore
        return resolve_effective_backend(agent_name)
    except ImportError:
        pass

    # Fallback: subprocess call
    result = subprocess.run(
        [sys.executable, _RESOLVE_SCRIPT, agent_name, "--dockerfile"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: resolve-agent-runtime.py failed for '{agent_name}':", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return "copilot-cli", None

    lines: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            lines[k.strip()] = v.strip()
    return lines.get("backend", "copilot-cli"), lines.get("dockerfile") or None


def build_agent(
    agent_name: str,
    *,
    dry_run: bool = False,
    platform_override: str | None = None,
) -> bool:
    """Build a single agent image. Returns True on success."""
    effective_backend, dockerfile_path = _resolve_backend(agent_name)

    if dockerfile_path is None or not Path(dockerfile_path).is_file():
        # Fall back to the default Dockerfile
        fallback = _REPO_ROOT / agent_name / "Dockerfile"
        if fallback.is_file():
            dockerfile_path = str(fallback)
            print(
                f"[build-agent] {agent_name}: no Dockerfile.{effective_backend} found; "
                f"using default Dockerfile"
            )
        else:
            print(
                f"ERROR: No Dockerfile found for agent '{agent_name}' "
                f"(backend: {effective_backend})",
                file=sys.stderr,
            )
            return False

    image = _image_name(agent_name)
    platform = (
        platform_override
        or _AGENT_PLATFORM_OVERRIDES.get(agent_name, "")
    )

    cmd = ["docker", "build"]
    if platform:
        cmd += ["--platform", platform]
    cmd += [
        "-f", dockerfile_path,
        "-t", image,
        str(_REPO_ROOT),
    ]

    print(f"[build-agent] {agent_name}: backend={effective_backend} image={image}")
    print(f"[build-agent] Command: {' '.join(cmd)}")

    if dry_run:
        print("[build-agent] DRY RUN — not executing.")
        return True

    result = subprocess.run(cmd, cwd=str(_REPO_ROOT))
    success = result.returncode == 0
    if success:
        print(f"[build-agent] {agent_name}: BUILD OK")
    else:
        print(f"[build-agent] {agent_name}: BUILD FAILED (exit {result.returncode})", file=sys.stderr)
    return success


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Constellation agent images using backend-aware Dockerfiles."
    )
    parser.add_argument(
        "agents",
        nargs="*",
        help="Agent name(s) to build (e.g. team-lead android). Omit with --all to build all agentic agents.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"Build all agentic agents: {', '.join(_AGENTIC_AGENTS)}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the docker build command without executing it.",
    )
    parser.add_argument(
        "--platform",
        default=None,
        help="Override the target platform (e.g. linux/amd64). Per-agent defaults still apply.",
    )
    args = parser.parse_args()

    targets: list[str] = args.agents or []
    if args.all:
        targets = list(_AGENTIC_AGENTS)

    if not targets:
        parser.print_help()
        sys.exit(1)

    failures = 0
    for agent_name in targets:
        ok = build_agent(agent_name, dry_run=args.dry_run, platform_override=args.platform)
        if not ok:
            failures += 1

    if failures:
        print(f"\n[build-agent] {failures} build(s) failed.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"\n[build-agent] All {len(targets)} build(s) succeeded.")


if __name__ == "__main__":
    main()
