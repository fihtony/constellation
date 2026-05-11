#!/usr/bin/env python3
"""Run a Constellation v2 agent locally (development mode).

Usage:
    python scripts/run_local.py compass
    python scripts/run_local.py team-lead
    python scripts/run_local.py web-dev
    python scripts/run_local.py code-review
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Ensure project root is in PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.session import InMemorySessionService
from framework.event_store import InMemoryEventStore
from framework.memory import InMemoryMemoryService
from framework.checkpoint import InMemoryCheckpointer
from framework.skills import SkillsRegistry
from framework.plugin import PluginManager
from framework.agent import AgentServices
from framework.task_store import InMemoryTaskStore
from framework.config import load_agent_config
from framework.permissions import PermissionEngine


# Agent registry
AGENTS = {
    "compass": ("agents.compass.agent", "CompassAgent", "compass_definition"),
    "team-lead": ("agents.team_lead.agent", "TeamLeadAgent", "team_lead_definition"),
    "web-dev": ("agents.web_dev.agent", "WebDevAgent", "web_dev_definition"),
    "code-review": ("agents.code_review.agent", "CodeReviewAgent", "code_review_definition"),
}

# Map agent IDs to permission profiles
_PERMISSION_PROFILES: dict[str, str] = {
    "web-dev": "development",
    "code-review": "read_only",
}


def create_services(
    agent_id: str,
    skills_dir: str = "skills",
) -> AgentServices:
    """Create shared services for local development.

    Injects TaskStore, loads PermissionEngine from config if available,
    and binds it to the global ToolRegistry.
    """
    skills_registry = SkillsRegistry()
    if os.path.isdir(skills_dir):
        skills_registry.load_directory(skills_dir)

    # Load agent config to display at startup
    config = load_agent_config(agent_id)
    print(f"[{agent_id}] Config loaded: runtime={config.get('runtime.backend')}, "
          f"model={config.get('runtime.model')}")

    # Bootstrap PermissionEngine from YAML profile
    profile = _PERMISSION_PROFILES.get(agent_id)
    if profile:
        perm_path = os.path.join("config", "permissions", f"{profile}.yaml")
        if os.path.isfile(perm_path):
            engine = PermissionEngine.from_yaml(perm_path)
            # Bind to global ToolRegistry
            from framework.tools.registry import get_registry
            get_registry().set_permission_engine(engine)
            print(f"[{agent_id}] Permission profile loaded: {profile}")

    return AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=skills_registry,
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=None,  # No LLM in local dev by default
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )


async def main():
    parser = argparse.ArgumentParser(description="Run a Constellation v2 agent locally")
    parser.add_argument("agent", choices=list(AGENTS.keys()), help="Agent to run")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--skills-dir", default="skills", help="Skills directory")
    args = parser.parse_args()

    agent_info = AGENTS[args.agent]
    module_path, class_name, def_name = agent_info

    # Dynamic import
    import importlib
    mod = importlib.import_module(module_path)
    agent_class = getattr(mod, class_name)
    agent_def = getattr(mod, def_name)

    services = create_services(args.agent, args.skills_dir)
    agent = agent_class(agent_def, services)

    print(f"[{args.agent}] Starting agent on port {args.port}...")
    await agent.start()
    print(f"[{args.agent}] Agent ready. Workflow compiled: {agent._compiled_workflow is not None}")

    # Start HTTP server
    from framework.a2a.server import A2ARequestHandler
    from http.server import HTTPServer

    class AgentHandler(A2ARequestHandler):
        _agent = agent
        _agent_id = agent_def.agent_id

    server = HTTPServer(("0.0.0.0", args.port), AgentHandler)
    print(f"[{args.agent}] HTTP server listening on 0.0.0.0:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{args.agent}] Shutting down...")
        await agent.stop()
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
