#!/usr/bin/env python3
"""Allowed real Tracker and SCM targets for integration tests.

This module is the single source of truth for real shared resources that the
agent regression scripts may touch.

Tracker agent tests:
- Read-only: fetch, search, myself on the one allowed ticket.
- Write-scoped: comment CRUD, field update restore, transition restore,
  assignee restore on the same allowed ticket only.

SCM agent tests:
- Read-only: repo resolution, repo listing, branch listing, PR inspection,
  PR comment listing, duplicate-comment checks on the one allowed repo only.
- Write-scoped: create feature branches, push files under agent-tests/,
  create PRs, and add comments on the same allowed repo only.
"""

from __future__ import annotations

import json
import os


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "agent_test_targets.json")


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _load_env_file() -> dict:
    """Load tests/.env and return key=value dict."""
    path = os.path.join(os.path.dirname(__file__), ".env")
    result = {}
    if not os.path.isfile(path):
        return result
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _parse_github_repo_url(url: str) -> tuple[str, str]:
    """Parse 'https://github.com/owner/repo' → (owner, repo)."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parts = [p for p in url.split("/") if p and ":" not in p]
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    return "", ""


CONFIG = _load_config()
_TEST_ENV = _load_env_file()

TRACKER_ALLOWED_TICKET = CONFIG["tracker"]["primaryTicket"]

# SCM config: prefer TEST_GITHUB_REPO_URL from tests/.env over agent_test_targets.json
# This way the json file can stay PII-free (generic placeholders only).
_github_repo_url = _TEST_ENV.get("TEST_GITHUB_REPO_URL", "").strip()
if _github_repo_url:
    _gh_owner, _gh_repo = _parse_github_repo_url(_github_repo_url)
    if _gh_owner and _gh_repo:
        _repo_url_clean = _github_repo_url.rstrip("/")
        SCM_ALLOWED_REPO: dict = {
            **CONFIG["scm"]["primaryRepo"],
            "owner": _gh_owner,
            "project": _gh_owner,
            "repo": _gh_repo,
            "browseUrl": _repo_url_clean,
            "cloneUrl": _repo_url_clean + ".git",
        }
    else:
        SCM_ALLOWED_REPO = CONFIG["scm"]["primaryRepo"]
else:
    SCM_ALLOWED_REPO = CONFIG["scm"]["primaryRepo"]


def tracker_ticket_key() -> str:
    return TRACKER_ALLOWED_TICKET["ticketKey"]


def tracker_ticket_url() -> str:
    return TRACKER_ALLOWED_TICKET["browseUrl"]


def scm_owner() -> str:
    """GitHub owner / Bitbucket project key."""
    return SCM_ALLOWED_REPO.get("owner") or SCM_ALLOWED_REPO.get("project", "")


def scm_project_key() -> str:
    """Alias for scm_owner(); kept for backward compatibility."""
    return SCM_ALLOWED_REPO.get("project") or SCM_ALLOWED_REPO.get("owner", "")


def scm_repo_slug() -> str:
    return SCM_ALLOWED_REPO["repo"]


def scm_repo_browse_url() -> str:
    return SCM_ALLOWED_REPO["browseUrl"]


def scm_clone_url() -> str:
    return SCM_ALLOWED_REPO["cloneUrl"]


def scm_search_query() -> str:
    return SCM_ALLOWED_REPO["searchQuery"]


def scm_write_root() -> str:
    return SCM_ALLOWED_REPO["writeRoot"]


def scm_pr_url(pr_id: int) -> str:
    return f"https://github.com/{scm_owner()}/{scm_repo_slug()}/pull/{int(pr_id)}"


def assert_tracker_write_allowed(ticket_key: str) -> None:
    if str(ticket_key or "").strip() != tracker_ticket_key():
        raise ValueError(
            f"WRITE to Tracker ticket '{ticket_key}' is forbidden. Allowed ticket: {tracker_ticket_key()}."
        )


def assert_scm_write_allowed(owner: str, repo: str, file_path: str = "") -> None:
    normalized_owner = str(owner or "").strip()
    normalized_repo = str(repo or "").strip()
    if normalized_owner != scm_owner() or normalized_repo != scm_repo_slug():
        raise ValueError(
            "WRITE to SCM repo "
            f"'{normalized_owner}/{normalized_repo}' is forbidden. "
            f"Allowed repo: {scm_owner()}/{scm_repo_slug()}."
        )
    if file_path:
        normalized_path = str(file_path).lstrip("/").replace("\\", "/")
        allowed_root = scm_write_root().replace("\\", "/")
        if not normalized_path.startswith(allowed_root):
            raise ValueError(
                f"WRITE path '{normalized_path}' is forbidden. Allowed root: {allowed_root}."
            )