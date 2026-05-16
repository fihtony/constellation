"""pytest configuration for v2 E2E tests.

Creates a full agent service set (all in-memory) suitable for end-to-end
workflow testing without real external services.

For real-service E2E tests, use fixtures from tests/integration/conftest.py.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_addoption(parser: "pytest.Parser") -> None:
    """Register E2E-specific CLI options."""
    parser.addoption(
        "--task",
        action="store",
        default="",
        help=(
            "Development task to send to Compass. "
            "Example: --task \"implement the jira ticket: https://jira.example.com/browse/PROJ-123\""
        ),
    )


def _load_test_env() -> dict[str, str]:
    env_file = Path(__file__).parent.parent / ".env"
    env: dict[str, str] = {}
    if not env_file.exists():
        return env
    with open(env_file, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip()
    return env


_TEST_ENV = _load_test_env()


def _env(key: str, default: str = "") -> str:
    return _TEST_ENV.get(key, os.environ.get(key, default))


@pytest.fixture(scope="session")
def agent_services():
    """Return a fully wired AgentServices instance (all in-memory)."""
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore

    return AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=None,
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )


@pytest.fixture(scope="session")
def llm_base_url() -> str:
    return _env("OPENAI_BASE_URL", "http://localhost:1288/v1")


@pytest.fixture(scope="session")
def llm_model() -> str:
    return _env("OPENAI_MODEL", "claude-haiku-4-5-20251001")


@pytest.fixture(scope="session")
def llm_available(llm_base_url) -> bool:
    """Return True if the LLM endpoint can be reached."""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{llm_base_url}/models",
            headers={"Accept": "application/json"},
        )
        api_key = _env("OPENAI_API_KEY")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False
