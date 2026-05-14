"""pytest configuration for v2 integration tests.

Loads credentials from tests/.env and creates session-scoped fixtures.
Tests are automatically skipped when the relevant credentials are absent.

Usage
-----
  pytest tests/integration/ -v             # skip unconfigured tests
  pytest tests/integration/ -v -m live     # only real-API tests
  pytest tests/integration/ -v -m "not live"  # skip real-API tests
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load tests/.env
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Jira fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def jira_token() -> str:
    val = _env("TEST_JIRA_TOKEN")
    if not val:
        pytest.skip("TEST_JIRA_TOKEN not set in tests/.env")
    return val


@pytest.fixture(scope="session")
def jira_email() -> str:
    val = _env("TEST_JIRA_EMAIL")
    if not val:
        pytest.skip("TEST_JIRA_EMAIL not set in tests/.env")
    return val


@pytest.fixture(scope="session")
def jira_ticket_url() -> str:
    val = _env("TEST_JIRA_TICKET_URL")
    if not val:
        pytest.skip("TEST_JIRA_TICKET_URL not set in tests/.env")
    return val


@pytest.fixture(scope="session")
def jira_client(jira_token, jira_email, jira_ticket_url):
    from agents.jira.client import JiraClient
    return JiraClient.from_ticket_url(
        ticket_url=jira_ticket_url,
        token=jira_token,
        email=jira_email,
    )


@pytest.fixture(scope="session")
def jira_provider(jira_client):
    """Wrap the JiraClient into a JiraRESTProvider for adapter tests."""
    from agents.jira.providers.rest import JiraRESTProvider
    provider = JiraRESTProvider.__new__(JiraRESTProvider)
    provider._client = jira_client
    return provider


@pytest.fixture(scope="session")
def jira_ticket_key(jira_ticket_url) -> str:
    from agents.jira.client import JiraClient
    return JiraClient.parse_ticket_key(jira_ticket_url)


# ---------------------------------------------------------------------------
# SCM / Bitbucket fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def scm_repo_url() -> str:
    val = _env("TEST_GITHUB_REPO_URL")
    if not val:
        pytest.skip("TEST_GITHUB_REPO_URL not set in tests/.env")
    return val


@pytest.fixture(scope="session")
def scm_token() -> str:
    val = _env("TEST_GITHUB_TOKEN")
    if not val:
        pytest.skip("TEST_GITHUB_TOKEN not set in tests/.env")
    return val


@pytest.fixture(scope="session")
def scm_client(scm_repo_url, scm_token):
    from agents.scm.client import BitbucketClient
    return BitbucketClient.from_repo_url(
        repo_url=scm_repo_url,
        token=scm_token,
    )


@pytest.fixture(scope="session")
def scm_project_repo(scm_repo_url) -> tuple[str, str]:
    from agents.scm.client import BitbucketClient
    return BitbucketClient.parse_project_repo(scm_repo_url)


# ---------------------------------------------------------------------------
# Figma fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def figma_token() -> str:
    val = _env("TEST_FIGMA_TOKEN")
    if not val:
        pytest.skip("TEST_FIGMA_TOKEN not set in tests/.env")
    return val


@pytest.fixture(scope="session")
def figma_file_url() -> str:
    val = _env("TEST_FIGMA_FILE_URL")
    if not val:
        pytest.skip("TEST_FIGMA_FILE_URL not set in tests/.env")
    return val


@pytest.fixture(scope="session")
def figma_client(figma_token):
    from agents.ui_design.clients.figma_rest import FigmaClient
    # Use minimal rate limiting in tests (1 second interval)
    return FigmaClient(token=figma_token, min_call_interval=1.0)


# ---------------------------------------------------------------------------
# LLM / connect-agent fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def llm_base_url() -> str:
    val = _env("OPENAI_BASE_URL", "http://localhost:1288/v1")
    return val


@pytest.fixture(scope="session")
def llm_model() -> str:
    return _env("OPENAI_MODEL", "gpt-5.4-mini")
