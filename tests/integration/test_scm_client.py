"""Integration tests for SCM clients (GitHub MCP + REST) against GitHub.

All tests call the real GitHub instance configured in tests/.env.
They are automatically skipped when TEST_SCM_REPO_URL / TEST_SCM_TOKEN
are absent.

Default backend: GitHubMCPProvider (github-mcp).

Run:
    pytest tests/integration/test_scm_client.py -v
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# TC-01: repo fetch
# ---------------------------------------------------------------------------

def test_scm_get_repo(scm_client, scm_project_repo):
    """GitHubMCPProvider.get_repo() returns the repo metadata dict."""
    owner, repo = scm_project_repo
    assert owner, "Could not parse owner from TEST_SCM_REPO_URL"
    assert repo, "Could not parse repo slug from TEST_SCM_REPO_URL"

    data, status = scm_client.get_repo(owner, repo)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(data, dict), "Expected a dict from get_repo()"
    assert (
        data.get("slug") == f"{owner}/{repo}"
        or data.get("repo") == repo
        or data.get("name") == repo
        or data.get("fullName") == f"{owner}/{repo}"
    ), f"Unexpected repo data: {data}"
    print(f"[scm-mcp] repo {owner}/{repo}: {data.get('description', '')[:60]}")


# ---------------------------------------------------------------------------
# TC-02: list branches
# ---------------------------------------------------------------------------

def test_scm_list_branches(scm_client, scm_project_repo):
    """GitHubMCPProvider.list_branches() returns at least one branch."""
    owner, repo = scm_project_repo
    branches, status = scm_client.list_branches(owner, repo)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(branches, list), "Expected a list of branches"
    assert len(branches) >= 1, f"No branches returned for {owner}/{repo}"
    names = [b.get("displayId") or b.get("name") or b.get("id", "") for b in branches]
    print(f"[scm-mcp] {len(branches)} branch(es): {names[:5]}")


# ---------------------------------------------------------------------------
# TC-03: list open PRs
# ---------------------------------------------------------------------------

def test_scm_list_prs(scm_client, scm_project_repo):
    """GitHubMCPProvider.list_prs() returns a list (possibly empty)."""
    owner, repo = scm_project_repo
    prs, status = scm_client.list_prs(owner, repo)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(prs, list), "Expected a list of PRs"
    print(f"[scm-mcp] {len(prs)} open PR(s)")


# ---------------------------------------------------------------------------
# TC-04: SCMAgentAdapter (direct mode)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scm_adapter_repo_inspect(scm_client, scm_project_repo):
    """SCMAgentAdapter in direct mode handles scm.repo.inspect (GitHub MCP)."""
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore
    from agents.scm.adapter import SCMAgentAdapter, scm_definition

    owner, repo = scm_project_repo

    services = AgentServices(
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
    adapter = SCMAgentAdapter(
        definition=scm_definition,
        services=services,
        scm_client=scm_client,
    )

    message = {
        "parts": [{"text": f"{owner}/{repo}"}],
        "metadata": {
            "requestedCapability": "scm.repo.inspect",
            "project": owner,
            "repo": repo,
        },
    }
    response = await adapter.handle_message(message)
    task_data = response.get("task", {})
    assert task_data.get("status", {}).get("state") == "TASK_STATE_COMPLETED"
    artifacts = task_data.get("artifacts", [])
    assert len(artifacts) >= 1
    import json
    result = json.loads(artifacts[0]["parts"][0]["text"])
    assert result.get("status") == "ok", f"Unexpected status: {result.get('status')}"
    print(f"[scm-mcp-adapter] repo inspect OK: {result.get('repo', {})}")
