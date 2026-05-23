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
from framework.env_utils import load_dotenv
from framework.launcher import get_launcher
from framework.registry_client import RegistryClient
from framework.runtime.adapter import get_runtime


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIMELESS_AGENTS = {"jira", "scm", "ui-design"}


# Agent registry
AGENTS = {
    "compass": ("agents.compass.agent", "CompassAgent", "compass_definition"),
    "team-lead": ("agents.team_lead.agent", "TeamLeadAgent", "team_lead_definition"),
    "web-dev": ("agents.web_dev.agent", "WebDevAgent", "web_dev_definition"),
    "code-review": ("agents.code_review.agent", "CodeReviewAgent", "code_review_definition"),
    "office": ("agents.office.agent", "OfficeAgent", "office_definition"),
    "jira": ("agents.jira.adapter", "JiraAgentAdapter", "jira_definition"),
    "scm": ("agents.scm.adapter", "SCMAgentAdapter", "scm_definition"),
    "ui-design": ("agents.ui_design.adapter", "UIDesignAgentAdapter", "ui_design_definition"),
}


def create_services(
    agent_id: str,
    skills_dir: str = "skills",
) -> AgentServices:
    """Create shared services for local development.

    Permission binding is handled by BaseAgent.start() via the agent's
    permission_profile in its AgentDefinition — no manual binding here.
    """
    load_dotenv(os.path.join(PROJECT_ROOT, "config", ".env"))
    load_dotenv(os.path.join(PROJECT_ROOT, "agents", agent_id.replace("-", "_"), ".env"))

    skills_registry = SkillsRegistry(skills_dir)
    skills_registry.load_all()

    # Load agent config to display at startup
    config = load_agent_config(agent_id)
    print(f"[{agent_id}] Config loaded: runtime={config.get('runtime.backend')}, "
          f"model={config.get('runtime.model')}")

    runtime = None
    if agent_id not in RUNTIMELESS_AGENTS:
        runtime = get_runtime(
            backend=config.get("runtime.backend") or config.get("runtime_backend"),
            model=config.get("runtime.model") or config.get("model"),
        )
    else:
        print(f"[{agent_id}] Runtime disabled: boundary adapter does not require agentic backend")

    return AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=skills_registry,
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=runtime,
        registry_client=RegistryClient.from_config(),
        task_store=InMemoryTaskStore(),
        launcher=get_launcher(),
    )


def main():
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
    asyncio.run(agent.start())
    print(f"[{args.agent}] Agent ready. Workflow compiled: {agent._compiled_workflow is not None}")

    # Start HTTP server
    from framework.a2a.server import A2ARequestHandler
    from http.server import ThreadingHTTPServer

    _card_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        module_path.replace(".", "/").rsplit("/", 1)[0],
        "agent-card.json",
    )
    _adv_url = f"http://localhost:{args.port}"

    _agent = agent

    class AgentHandler(A2ARequestHandler):
        agent = _agent  # noqa: F811
        advertised_url = _adv_url
        agent_card_path = _card_path

    server = ThreadingHTTPServer(("0.0.0.0", args.port), AgentHandler)
    print(f"[{args.agent}] HTTP server listening on 0.0.0.0:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{args.agent}] Shutting down...")
        asyncio.run(agent.stop())
        server.shutdown()


if __name__ == "__main__":
    main()
