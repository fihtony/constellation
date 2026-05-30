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
from pathlib import Path
import sys

# Ensure project root is in PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.a2a.protocol import TaskState
from framework.session import InMemorySessionService, SqliteSessionService
from framework.event_store import InMemoryEventStore, SqliteEventStore
from framework.memory import InMemoryMemoryService
from framework.checkpoint import InMemoryCheckpointer, SqliteCheckpointer
from framework.skills import SkillsRegistry
from framework.plugin import PluginManager
from framework.agent import AgentServices
from framework.task_store import InMemoryTaskStore, SqliteTaskStore
from framework.config import load_agent_config, validate_startup_config
from framework.env_utils import load_agent_environment
from framework.launcher import get_launcher
from framework.registry_client import RegistryClient
from framework.runtime.adapter import get_runtime


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RECOVERABLE_TASK_STATES = {
    TaskState.SUBMITTED,
    TaskState.ROUTING,
    TaskState.DISPATCHED,
    TaskState.WORKING,
}


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


def _uses_persistent_state(config) -> bool:
    return str(config.get("execution_mode", "per-task") or "per-task").strip().lower() == "persistent"


def _state_root_for_agent(agent_id: str, config) -> Path:
    artifact_root = str(os.environ.get("ARTIFACT_ROOT", "") or "").strip()
    if artifact_root:
        base_dir = Path(artifact_root) / ".agent-state"
    else:
        configured_data_dir = str(config.get("data.directory", "data") or "data").strip()
        data_dir = Path(configured_data_dir)
        if not data_dir.is_absolute():
            data_dir = Path(PROJECT_ROOT) / data_dir
        base_dir = data_dir / "agent-state"
    state_dir = base_dir / agent_id
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _recover_orphaned_tasks(agent_id: str, task_store: SqliteTaskStore) -> None:
    recovered = 0
    for task in task_store.list_tasks(agent_id=agent_id, limit=10_000):
        if task.status.state not in _RECOVERABLE_TASK_STATES:
            continue
        task_store.update_state(
            task.id,
            TaskState.FAILED,
            "Agent restarted before completing this task; please retry.",
        )
        recovered += 1
    if recovered:
        print(f"[{agent_id}] Recovered {recovered} interrupted task(s) from persistent state")


def _create_state_backends(agent_id: str, config):
    if not _uses_persistent_state(config):
        return (
            InMemorySessionService(),
            InMemoryEventStore(),
            InMemoryCheckpointer(),
            InMemoryTaskStore(),
        )

    state_dir = _state_root_for_agent(agent_id, config)
    task_store = SqliteTaskStore(db_path=str(state_dir / "tasks.db"))
    _recover_orphaned_tasks(agent_id, task_store)
    return (
        SqliteSessionService(db_path=str(state_dir / "sessions.db")),
        SqliteEventStore(db_path=str(state_dir / "events.db")),
        SqliteCheckpointer(db_path=str(state_dir / "checkpoints.db")),
        task_store,
    )


def create_services(
    agent_id: str,
    skills_dir: str = "skills",
) -> AgentServices:
    """Create shared services for local development.

    Permission binding is handled by BaseAgent.start() via the agent's
    permission_profile in its AgentDefinition — no manual binding here.
    """
    bootstrap_config = load_agent_config(agent_id)
    runtime_env_required = bool(bootstrap_config.get("runtime_env_required", False))

    load_agent_environment(PROJECT_ROOT, agent_id, include_runtime_env=runtime_env_required)

    # Startup consistency check — fail fast on misconfiguration.
    # Skip credential checks for boundary agents that don't need the shared runtime.
    try:
        warnings = validate_startup_config(
            skip_credential_check=not runtime_env_required,
            agent_id=agent_id,
        )
        for w in warnings:
            print(f"[{agent_id}] WARNING: {w}")
    except Exception as exc:  # ConfigValidationError
        print(f"[{agent_id}] STARTUP VALIDATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    skills_registry = SkillsRegistry(skills_dir)
    skills_registry.load_all()

    # Load agent config to display at startup
    config = load_agent_config(agent_id)
    print(f"[{agent_id}] Config loaded: runtime={config.get('runtime.backend')}, "
          f"model={config.get('runtime.model')}")

    runtime = None
    if runtime_env_required:
        runtime = get_runtime(
            backend=config.get("runtime.backend") or config.get("runtime_backend"),
            model=config.get("runtime.model") or config.get("model"),
        )
    else:
        print(f"[{agent_id}] Runtime disabled: shared runtime env not required for this agent")

    session_service, event_store, checkpoint_service, task_store = _create_state_backends(
        agent_id,
        config,
    )

    return AgentServices(
        session_service=session_service,
        event_store=event_store,
        memory_service=InMemoryMemoryService(),
        skills_registry=skills_registry,
        plugin_manager=PluginManager(),
        checkpoint_service=checkpoint_service,
        runtime=runtime,
        registry_client=RegistryClient.from_config(),
        task_store=task_store,
        launcher=get_launcher(),
    )


def main():
    try:
        default_port = int(os.environ.get("PORT", "8000") or "8000")
    except ValueError:
        default_port = 8000

    parser = argparse.ArgumentParser(description="Run a Constellation v2 agent locally")
    parser.add_argument("agent", choices=list(AGENTS.keys()), help="Agent to run")
    parser.add_argument("--port", type=int, default=default_port, help="HTTP port (default: PORT env or 8000)")
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
