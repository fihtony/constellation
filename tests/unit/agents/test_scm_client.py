"""Unit tests for BitbucketClient URL parsing (v2).

Covers both project-based and user-based repo URL formats.
"""
from __future__ import annotations

import pytest

from agents.scm.client import BitbucketClient, _parse_bb_project_repo


class TestParseProjectRepo:
    """Verify _parse_bb_project_repo handles all Bitbucket URL formats."""

    def test_project_repo_url(self):
        url = "https://bitbucket.corp.com/projects/PROJ/repos/my-repo/browse"
        host, project, repo = _parse_bb_project_repo(url)
        assert host == "https://bitbucket.corp.com"
        assert project == "PROJ"
        assert repo == "my-repo"

    def test_user_repo_url(self):
        url = "https://bitbucket.example.com/users/test1/repos/web-ui-test/browse"
        host, project, repo = _parse_bb_project_repo(url)
        assert host == "https://bitbucket.example.com"
        assert project == "~test1"
        assert repo == "web-ui-test"

    def test_user_repo_no_browse_suffix(self):
        url = "https://bb.example.com/users/jdoe/repos/my-app"
        host, project, repo = _parse_bb_project_repo(url)
        assert host == "https://bb.example.com"
        assert project == "~jdoe"
        assert repo == "my-app"

    def test_unknown_format_returns_empty(self):
        url = "https://github.com/org/repo"
        host, project, repo = _parse_bb_project_repo(url)
        assert host == "https://github.com"
        assert project == ""
        assert repo == ""


class TestParseProjectRepoClassMethod:
    """Verify the classmethod wrapper returns (project, repo) only."""

    def test_project_url(self):
        url = "https://bitbucket.corp.com/projects/MY/repos/android-test/browse"
        project, repo = BitbucketClient.parse_project_repo(url)
        assert project == "MY"
        assert repo == "android-test"

    def test_user_url(self):
        url = "https://bitbucket.example.com/users/test1/repos/web-ui-test/browse"
        project, repo = BitbucketClient.parse_project_repo(url)
        assert project == "~test1"
        assert repo == "web-ui-test"
