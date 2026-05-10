"""Integration tests for BitbucketClient against Bitbucket Server REST API.

All tests call the real Bitbucket Server instance configured in tests/.env.
They are automatically skipped when TEST_GITHUB_REPO_URL / TEST_GITHUB_TOKEN
are absent.

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
    """BitbucketClient.get_repo() returns the repo metadata dict."""
    project, repo = scm_project_repo
    assert project, "Could not parse project from TEST_GITHUB_REPO_URL"
    assert repo, "Could not parse repo slug from TEST_GITHUB_REPO_URL"

    data, status = scm_client.get_repo(project, repo)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(data, dict), "Expected a dict from get_repo()"
    assert data.get("slug") == repo or data.get("name") == repo, (
        f"Unexpected repo data: slug={data.get('slug')!r}, name={data.get('name')!r}"
    )
    print(f"[scm] repo {project}/{repo}: {data.get('description', '')[:60]}")


# ---------------------------------------------------------------------------
# TC-02: list branches
# ---------------------------------------------------------------------------

def test_scm_list_branches(scm_client, scm_project_repo):
    """BitbucketClient.list_branches() returns at least one branch."""
    project, repo = scm_project_repo
    branches, status = scm_client.list_branches(project, repo)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(branches, list), "Expected a list of branches"
    assert len(branches) >= 1, f"No branches returned for {project}/{repo}"
    names = [b.get("displayId") or b.get("id", "") for b in branches]
    print(f"[scm] {len(branches)} branch(es): {names[:5]}")


# ---------------------------------------------------------------------------
# TC-03: list open PRs
# ---------------------------------------------------------------------------

def test_scm_list_prs(scm_client, scm_project_repo):
    """BitbucketClient.list_prs() returns a list (possibly empty)."""
    project, repo = scm_project_repo
    prs, status = scm_client.list_prs(project, repo)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(prs, list), "Expected a list of PRs"
    print(f"[scm] {len(prs)} open PR(s)")


# ---------------------------------------------------------------------------
# TC-04: SCMAgentAdapter (direct mode)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scm_adapter_repo_inspect(scm_client, scm_project_repo):
    """SCMAgentAdapter in direct mode handles scm.repo.inspect."""
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from agents.scm.adapter import SCMAgentAdapter, scm_definition

    project, repo = scm_project_repo

    services = AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=None,
        registry_client=None,
    )
    adapter = SCMAgentAdapter(
        definition=scm_definition,
        services=services,
        dispatch_mode="direct",
        scm_client=scm_client,
    )

    message = {
        "parts": [{"text": f"{project}/{repo}"}],
        "metadata": {
            "requestedCapability": "scm.repo.inspect",
            "project": project,
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
    print(f"[scm-adapter] repo inspect OK: {result.get('repo', {}).get('slug', '')}")
