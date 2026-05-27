"""Unit tests for BitbucketClient URL parsing (v2).

Covers both project-based and user-based repo URL formats.
"""
from __future__ import annotations

import subprocess

import pytest

from agents.scm.adapter import SCMAgentAdapter
from agents.scm.client import BitbucketClient, _parse_bb_project_repo
from agents.scm.providers.github_mcp import GitHubMCPProvider


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


class TestScmAdapterCloneBehavior:
    def test_build_auth_header_returns_empty_when_token_missing(self, monkeypatch):
        monkeypatch.delenv("SCM_TOKEN", raising=False)
        monkeypatch.delenv("SCM_USERNAME", raising=False)

        adapter = object.__new__(SCMAgentAdapter)

        assert adapter._build_auth_header("https://github.com/fihtony/english-study-hub.git") == ""

    def test_handle_clone_retries_directory_creation_failure(self, monkeypatch, tmp_path):
        target_path = tmp_path / "task" / "scm" / "english-study-hub"
        repo_url = "https://github.com/fihtony/english-study-hub"
        calls = {"count": 0}

        monkeypatch.setenv("SCM_TOKEN", "token")
        monkeypatch.delenv("SCM_USERNAME", raising=False)
        monkeypatch.setattr("agents.scm.adapter.build_isolated_git_env", lambda scope: {})
        monkeypatch.setattr("agents.scm.adapter.time.sleep", lambda *_args, **_kwargs: None)

        def _run(cmd, capture_output, text, timeout, env):
            calls["count"] += 1
            if calls["count"] == 1:
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    stdout="",
                    stderr=(
                        "fatal: could not create leading directories of "
                        f"'{target_path}': Operation not permitted"
                    ),
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("agents.scm.adapter.subprocess.run", _run)

        adapter = object.__new__(SCMAgentAdapter)
        result = adapter._handle_clone({"repoUrl": repo_url, "targetPath": str(target_path)})

        assert result["cloned"] is True
        assert result["status"] == "ok"
        assert calls["count"] == 2


class TestScmAdapterPrEvidenceCapabilities:
    def test_dispatch_upload_pr_image_calls_client(self, tmp_path):
        image_path = tmp_path / "screen.png"
        image_path.write_bytes(b"png")
        calls = {}

        class FakeClient:
            def upload_issue_image(self, owner, repo, issue_number, path, filename="", task_id=""):
                calls.update({
                    "owner": owner,
                    "repo": repo,
                    "issue_number": issue_number,
                    "path": path,
                    "filename": filename,
                    "task_id": task_id,
                })
                return {"href": "https://cdn.example/screen.png", "asset_id": 123}, "ok"

        adapter = object.__new__(SCMAgentAdapter)
        adapter._get_client = lambda: FakeClient()  # type: ignore[attr-defined]

        result = adapter._dispatch(
            "scm.pr.image.upload",
            str(image_path),
            {"metadata": {
                "project": "org",
                "repo": "repo",
                "prNumber": 42,
                "imagePath": str(image_path),
                "filename": "custom.png",
                "task_id": "task-123",
            }},
        )

        assert result["ok"] is True
        assert result["image_url"] == "https://cdn.example/screen.png"
        assert calls == {
            "owner": "org",
            "repo": "repo",
            "issue_number": 42,
            "path": str(image_path),
            "filename": "custom.png",
            "task_id": "task-123",
        }

    def test_dispatch_update_pr_calls_client(self):
        calls = {}

        class FakeClient:
            def update_pr(self, owner, repo, pr_id, body=None, title=None):
                calls.update({
                    "owner": owner,
                    "repo": repo,
                    "pr_id": pr_id,
                    "body": body,
                    "title": title,
                })
                return {"id": pr_id, "url": "https://github.com/org/repo/pull/42"}, "ok"

        adapter = object.__new__(SCMAgentAdapter)
        adapter._get_client = lambda: FakeClient()  # type: ignore[attr-defined]

        result = adapter._dispatch(
            "scm.pr.update",
            "Updated body",
            {"metadata": {
                "project": "org",
                "repo": "repo",
                "prNumber": 42,
                "description": "Updated body",
                "title": "Updated title",
            }},
        )

        assert result["ok"] is True
        assert calls == {
            "owner": "org",
            "repo": "repo",
            "pr_id": 42,
            "body": "Updated body",
            "title": "Updated title",
        }


class TestGitHubMCPProviderPrEvidenceCompatibility:
    def test_update_pr_delegates_to_rest_client(self, monkeypatch):
        calls = {}

        class FakeGitHubClient:
            def __init__(self, token=""):
                calls["token"] = token

            def update_pr(self, owner, repo, pr_id, body="", title=None, timeout=20):
                calls.update({
                    "owner": owner,
                    "repo": repo,
                    "pr_id": pr_id,
                    "body": body,
                    "title": title,
                    "timeout": timeout,
                })
                return {"id": pr_id}, "ok"

        monkeypatch.setattr("agents.scm.client.GitHubClient", FakeGitHubClient)

        data, status = GitHubMCPProvider(token="token-123").update_pr(
            "org", "repo", 42, body="Updated body", title="Updated title", timeout=30
        )

        assert status == "ok"
        assert data == {"id": 42}
        assert calls == {
            "token": "token-123",
            "owner": "org",
            "repo": "repo",
            "pr_id": 42,
            "body": "Updated body",
            "title": "Updated title",
            "timeout": 30,
        }

    def test_upload_issue_image_delegates_to_rest_client(self, monkeypatch, tmp_path):
        image_path = tmp_path / "screen.png"
        image_path.write_bytes(b"png")
        calls = {}

        class FakeGitHubClient:
            def __init__(self, token=""):
                calls["token"] = token

            def upload_issue_image(
                self,
                owner,
                repo,
                issue_number,
                path,
                filename="",
                task_id="",
                timeout=60,
            ):
                calls.update({
                    "owner": owner,
                    "repo": repo,
                    "issue_number": issue_number,
                    "path": path,
                    "filename": filename,
                    "task_id": task_id,
                    "timeout": timeout,
                })
                return {"href": "https://cdn.example/screen.png"}, "ok"

        monkeypatch.setattr("agents.scm.client.GitHubClient", FakeGitHubClient)

        data, status = GitHubMCPProvider(token="token-123").upload_issue_image(
            "org",
            "repo",
            42,
            str(image_path),
            filename="custom.png",
            task_id="task-123",
            timeout=30,
        )

        assert status == "ok"
        assert data == {"href": "https://cdn.example/screen.png"}
        assert calls == {
            "token": "token-123",
            "owner": "org",
            "repo": "repo",
            "issue_number": 42,
            "path": str(image_path),
            "filename": "custom.png",
            "task_id": "task-123",
            "timeout": 30,
        }
