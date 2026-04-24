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


CONFIG = _load_config()

TRACKER_ALLOWED_TICKET = CONFIG["tracker"]["primaryTicket"]

SCM_ALLOWED_REPO = CONFIG["scm"]["primaryRepo"]


def tracker_ticket_key() -> str:
    return TRACKER_ALLOWED_TICKET["ticketKey"]


def tracker_ticket_url() -> str:
    return TRACKER_ALLOWED_TICKET["browseUrl"]


def scm_project_key() -> str:
    return SCM_ALLOWED_REPO["project"]


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
    return (
        f"https://scm.example.com/projects/{scm_project_key()}/"
        f"repos/{scm_repo_slug()}/pull-requests/{int(pr_id)}"
    )


def assert_tracker_write_allowed(ticket_key: str) -> None:
    if str(ticket_key or "").strip() != tracker_ticket_key():
        raise ValueError(
            f"WRITE to Tracker ticket '{ticket_key}' is forbidden. Allowed ticket: {tracker_ticket_key()}."
        )


def assert_scm_write_allowed(project: str, repo: str, file_path: str = "") -> None:
    normalized_project = str(project or "").strip()
    normalized_repo = str(repo or "").strip()
    if normalized_project != scm_project_key() or normalized_repo != scm_repo_slug():
        raise ValueError(
            "WRITE to SCM repo "
            f"'{normalized_project}/{normalized_repo}' is forbidden. "
            f"Allowed repo: {scm_project_key()}/{scm_repo_slug()}."
        )
    if file_path:
        normalized_path = str(file_path).lstrip("/").replace("\\", "/")
        allowed_root = scm_write_root().replace("\\", "/")
        if not normalized_path.startswith(allowed_root):
            raise ValueError(
                f"WRITE path '{normalized_path}' is forbidden. Allowed root: {allowed_root}."
            )