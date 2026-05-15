"""pytest configuration for v2 integration tests.

Loads credentials from tests/.env and creates session-scoped fixtures.
Tests are automatically skipped when the relevant credentials are absent.

Default backends:
  SCM   → GitHub MCP  (GitHubMCPProvider)   — requires TEST_SCM_TOKEN (alias: TEST_GITHUB_TOKEN)
                                              and TEST_SCM_REPO_URL  (alias: TEST_GITHUB_REPO_URL)
  Jira  → Atlassian Rovo MCP (JiraMCPProvider) — requires TEST_JIRA_TOKEN + TEST_JIRA_EMAIL
  Stitch → Google Stitch MCP (StitchMcpClient) — requires TEST_STITCH_API_KEY

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
# Jira fixtures — default to MCP backend
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
def jira_base_url(jira_ticket_url) -> str:
    """Extract the Jira site root URL from the ticket URL."""
    if "/browse/" in jira_ticket_url:
        return jira_ticket_url.split("/browse/")[0].rstrip("/")
    return jira_ticket_url.rstrip("/")


@pytest.fixture(scope="session")
def jira_ticket_key(jira_ticket_url) -> str:
    if "/browse/" in jira_ticket_url:
        return jira_ticket_url.split("/browse/")[1].split("/")[0].split("?")[0].strip()
    return ""


@pytest.fixture(scope="session")
def jira_provider(jira_token, jira_email, jira_base_url):
    """JiraMCPProvider — the default Jira backend for v2 integration tests."""
    from agents.jira.providers.mcp import JiraMCPProvider
    return JiraMCPProvider(
        base_url=jira_base_url,
        token=jira_token,
        email=jira_email,
    )


@pytest.fixture(scope="session")
def jira_rest_provider(jira_token, jira_email, jira_base_url):
    """JiraRESTProvider — direct REST backend, available for comparison tests."""
    from agents.jira.providers.rest import JiraRESTProvider
    from agents.jira.client import JiraClient
    client = JiraClient(
        base_url=jira_base_url,
        token=jira_token,
        email=jira_email,
    )
    provider = JiraRESTProvider.__new__(JiraRESTProvider)
    provider._client = client
    return provider


@pytest.fixture(scope="session")
def jira_client(jira_token, jira_email, jira_ticket_url):
    """Legacy JiraClient fixture kept for backward-compat with existing tests."""
    from agents.jira.client import JiraClient
    return JiraClient.from_ticket_url(
        ticket_url=jira_ticket_url,
        token=jira_token,
        email=jira_email,
    )


# ---------------------------------------------------------------------------
# SCM fixtures — default to GitHub MCP backend
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def scm_repo_url() -> str:
    val = _env("TEST_SCM_REPO_URL") or _env("TEST_GITHUB_REPO_URL")
    if not val:
        pytest.skip("TEST_SCM_REPO_URL (or TEST_GITHUB_REPO_URL) not set in tests/.env")
    return val


@pytest.fixture(scope="session")
def scm_token() -> str:
    val = _env("TEST_SCM_TOKEN") or _env("TEST_GITHUB_TOKEN")
    if not val:
        pytest.skip("TEST_SCM_TOKEN (or TEST_GITHUB_TOKEN) not set in tests/.env")
    return val


def _parse_github_owner_repo(url: str) -> tuple[str, str]:
    """Parse (owner, repo) from a GitHub repo URL."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parts = [p for p in url.split("/") if p and ":" not in p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", ""


@pytest.fixture(scope="session")
def scm_owner(scm_repo_url) -> str:
    owner, _ = _parse_github_owner_repo(scm_repo_url)
    return owner


@pytest.fixture(scope="session")
def scm_repo_name(scm_repo_url) -> str:
    _, repo = _parse_github_owner_repo(scm_repo_url)
    return repo


@pytest.fixture(scope="session")
def scm_client(scm_token):
    """GitHubMCPProvider — the default SCM client for v2 integration tests."""
    from agents.scm.providers.github_mcp import GitHubMCPProvider
    return GitHubMCPProvider(token=scm_token)


@pytest.fixture(scope="session")
def scm_rest_client(scm_token):
    """GitHubClient — GitHub REST API backend, available for comparison tests."""
    from agents.scm.client import GitHubClient
    return GitHubClient(token=scm_token)


@pytest.fixture(scope="session")
def scm_project_repo(scm_owner, scm_repo_name) -> tuple[str, str]:
    """Return (owner, repo_name) — equivalent of Bitbucket (project, repo)."""
    return scm_owner, scm_repo_name


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
# Google Stitch MCP fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def stitch_api_key() -> str:
    val = _env("TEST_STITCH_API_KEY")
    if not val:
        pytest.skip("TEST_STITCH_API_KEY not set in tests/.env")
    return val


@pytest.fixture(scope="session")
def stitch_project_url() -> str:
    val = _env("TEST_STITCH_PROJECT_URL")
    if not val:
        pytest.skip("TEST_STITCH_PROJECT_URL not set in tests/.env")
    return val


@pytest.fixture(scope="session")
def stitch_project_id(stitch_project_url) -> str:
    """Extract project ID from a Stitch project URL."""
    if "/projects/" in stitch_project_url:
        after = stitch_project_url.split("/projects/")[1]
        return after.split("/")[0].split("?")[0]
    return stitch_project_url


@pytest.fixture(scope="session")
def stitch_screen_id() -> str:
    return _env("TEST_STITCH_SCREEN_ID", "")


@pytest.fixture(scope="session")
def stitch_client(stitch_api_key):
    """StitchMcpClient — Google Stitch MCP backend."""
    from agents.ui_design.clients.stitch_mcp import StitchMcpClient
    return StitchMcpClient(api_key=stitch_api_key)


# ---------------------------------------------------------------------------
# LLM / connect-agent fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def llm_base_url() -> str:
    val = _env("OPENAI_BASE_URL", "http://localhost:1288/v1")
    return val


@pytest.fixture(scope="session")
def llm_model() -> str:
    return _env("OPENAI_MODEL", "gpt-5-mini")
