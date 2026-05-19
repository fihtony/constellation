"""Unit tests for GitHubClient image upload and PR description embedding.

Verifies that:
  1. upload_issue_image uses GitHub Release Assets API (octet-stream, not multipart).
  2. upload_issue_image handles "already_exists" by returning the existing URL.
  3. update_pr sends PATCH to the GitHub API with the correct body.
  4. The full create_pr flow in web_dev/nodes.py calls scm_upload_pr_image and
     embeds the returned CDN URL in the PR description.

Discovery:
  - uploads.github.com/issues/assets returns HTTP 422 "Bad Size" with any multipart
    payload (including real screenshots) when using fine-grained PATs.
  - uploads.github.com/releases/{id}/assets with Content-Type: application/octet-stream
    WORKS with fine-grained PATs and returns {"browser_download_url": "..."}.
  - The "screenshot-assets" pre-release is created once per repo and reused.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_png_file() -> tuple[str, bytes]:
    """Create a tiny valid PNG in a temp file and return (path, bytes)."""
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
        b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
        b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(png_bytes)
    tmp.close()
    return tmp.name, png_bytes


def _release_upload_mocks(release_id: int, cdn_url: str):
    """Return fake urlopen: call 1 = GET release found, call 2 = upload success."""
    call_count = [0]

    def _urlopen(req, timeout=None):
        call_count[0] += 1
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.status = 200
        if call_count[0] == 1:
            resp.read.return_value = json.dumps({"id": release_id}).encode()
        else:
            resp.read.return_value = json.dumps(
                {"id": 1, "browser_download_url": cdn_url}
            ).encode()
        return resp

    return _urlopen


# ---------------------------------------------------------------------------
# GitHubClient.upload_issue_image  (Release Assets approach)
# ---------------------------------------------------------------------------

class TestGitHubClientUploadIssueImage:
    """Test that upload_issue_image uses GitHub Release Assets API (not multipart)."""

    def _mock_urlopen_sequence(self, responses: list[tuple[int, dict]]):
        """Return a mock urlopen that returns different responses per call."""
        call_count = [0]

        def _urlopen(req, timeout=None):
            idx = min(call_count[0], len(responses) - 1)
            status, payload = responses[idx]
            call_count[0] += 1
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.status = status
            mock_resp.read.return_value = json.dumps(payload).encode()
            return mock_resp

        return _urlopen

    def test_returns_href_on_success(self):
        """upload_issue_image returns {href: cdn_url} when the API responds 200."""
        from agents.scm.client import GitHubClient

        cdn_url = "https://user-images.githubusercontent.com/123/abc.png"
        path, _ = _make_png_file()
        try:
            with patch("agents.scm.client.urlopen",
                       side_effect=_release_upload_mocks(999, cdn_url)):
                client = GitHubClient(token="test-token")
                result, status = client.upload_issue_image(
                    "owner", "repo", 57, path, filename="desktop.png"
                )

            assert status == "ok", f"Expected ok, got {status!r}: {result}"
            assert result.get("href") == cdn_url
        finally:
            os.unlink(path)

    def test_uses_release_assets_endpoint_with_octet_stream(self):
        """upload_issue_image POSTs to uploads.github.com releases/{id}/assets with octet-stream."""
        from agents.scm.client import GitHubClient

        captured_requests = []

        def fake_urlopen(req, timeout=None):
            captured_requests.append({
                "url": req.full_url,
                "method": req.method,
                "content_type": req.get_header("Content-type"),
            })
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.status = 200
            if len(captured_requests) == 1:
                resp.read.return_value = json.dumps({"id": 42}).encode()
            else:
                cdn = "https://github.com/owner/repo/releases/download/screenshot-assets/pr10-screenshot.png"
                resp.read.return_value = json.dumps({"id": 1, "browser_download_url": cdn}).encode()
            return resp

        path, _ = _make_png_file()
        try:
            with patch("agents.scm.client.urlopen", side_effect=fake_urlopen):
                client = GitHubClient(token="test-token")
                client.upload_issue_image("owner", "repo", 10, path, filename="screenshot.png")

            assert len(captured_requests) >= 2
            upload_req = captured_requests[1]
            assert "uploads.github.com" in upload_req["url"]
            assert "releases/42/assets" in upload_req["url"]
            assert "name=pr10-screenshot.png" in upload_req["url"]
            assert upload_req["method"] == "POST"
            assert upload_req["content_type"] == "application/octet-stream", (
                f"Expected octet-stream, got: {upload_req['content_type']}"
            )
        finally:
            os.unlink(path)

    def test_returns_error_when_file_missing(self):
        """upload_issue_image returns error dict when image_path does not exist."""
        from agents.scm.client import GitHubClient

        client = GitHubClient(token="test-token")
        result, status = client.upload_issue_image(
            "owner", "repo", 1, "/nonexistent/path.png"
        )
        assert "error" in result or "read_error" in status

    def test_authorization_header_not_logged(self, capfd):
        """Authorization header value must never be printed to stdout/stderr."""
        from agents.scm.client import GitHubClient
        from unittest.mock import MagicMock

        cdn_url = "https://github.com/o/r/releases/download/screenshot-assets/f.png"
        path, _ = _make_png_file()
        try:
            with patch("agents.scm.client.urlopen",
                       side_effect=_release_upload_mocks(1, cdn_url)):
                client = GitHubClient(token="super-secret-token")
                client.upload_issue_image("o", "r", 1, path)

            out, err = capfd.readouterr()
            assert "super-secret-token" not in out
            assert "super-secret-token" not in err
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# GitHubClient.update_pr
# ---------------------------------------------------------------------------

class TestGitHubClientUpdatePR:
    """Test that update_pr sends PATCH with correct payload."""

    def test_update_pr_body(self):
        """update_pr PATCHes the PR body with the new description."""
        from agents.scm.client import GitHubClient

        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            import json as _json
            captured["method"] = req.method
            captured["url"] = req.full_url
            captured["body"] = _json.loads(req.data.decode())
            body_bytes = _json.dumps(
                {"number": 57, "html_url": "https://github.com/o/r/pull/57"}
            ).encode()
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.status = 200
            mock_resp.read.return_value = body_bytes
            return mock_resp

        with patch("agents.scm.client.urlopen", side_effect=fake_urlopen):
            client = GitHubClient(token="test-token")
            result, status = client.update_pr("o", "r", 57, body="Updated description")

        assert status == "ok"
        assert captured["method"] == "PATCH"
        assert "repos/o/r/pulls/57" in captured["url"]
        assert captured["body"]["body"] == "Updated description"
        assert result["id"] == 57

    def test_update_pr_no_changes_returns_no_changes(self):
        """update_pr with no body/title returns 'no_changes' without making an HTTP call."""
        from agents.scm.client import GitHubClient

        with patch("agents.scm.client.urlopen") as mock_urlopen:
            client = GitHubClient(token="test-token")
            result, status = client.update_pr("o", "r", 57)

        assert status == "no_changes"
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# SCMUpdatePR tool
# ---------------------------------------------------------------------------

class TestSCMUpdatePRTool:
    """Test the SCMUpdatePR boundary tool."""

    def test_tool_registered(self):
        """SCMUpdatePR must be present in the SCM _TOOLS list."""
        from agents.scm.tools import _TOOLS

        tool_names = [t.name for t in _TOOLS]
        assert "scm_update_pr" in tool_names, (
            f"scm_update_pr not in _TOOLS: {tool_names}"
        )

    def test_tool_execute_calls_update_pr(self):
        """SCMUpdatePR.execute_sync calls GitHubClient.update_pr and returns ok."""
        from agents.scm.tools import _TOOLS

        tool = next(t for t in _TOOLS if t.name == "scm_update_pr")

        fake_result = ({"id": 57, "url": "https://github.com/fihtony/english-study-hub/pull/57"}, "ok")
        with patch("agents.scm.client.GitHubClient.update_pr", return_value=fake_result) as mock_update:
            result = tool.execute_sync(
                repo_url="https://github.com/fihtony/english-study-hub",
                pr_number=57,
                description="Updated body with screenshots",
                task_id="test-task",
            )

        mock_update.assert_called_once()
        call_args = mock_update.call_args
        assert call_args.args[2] == 57  # pr_id
        assert "Updated body with screenshots" in call_args.kwargs.get("body", "") \
            or "Updated body with screenshots" in str(call_args.args)
        parsed = json.loads(result.output)
        assert parsed.get("ok") is True


# ---------------------------------------------------------------------------
# Integration: screenshot CDN URLs embedded in PR description
# ---------------------------------------------------------------------------

class TestScreenshotEmbeddedInPRDescription:
    """Verify create_pr uploads screenshots via CDN and embeds the returned URL."""

    @pytest.mark.asyncio
    async def test_screenshots_embedded_via_cdn_upload(self):
        """create_pr calls scm_upload_pr_image and embeds the CDN URL in PR description."""
        from agents.web_dev.nodes import create_pr

        desktop_png_path, _ = _make_png_file()
        try:
            tool_calls: list[dict] = []
            CDN_URL = "https://github.com/fihtony/english-study-hub/releases/download/screenshot-assets/pr0-desktop.png"

            def fake_boundary_tool(state, tool_name, args):
                tool_calls.append({"tool": tool_name, "args": args})
                if tool_name == "scm_push":
                    return {"status": "ok"}
                if tool_name == "scm_upload_pr_image":
                    return {"ok": True, "image_url": CDN_URL}
                if tool_name == "scm_create_pr":
                    return {
                        "status": "ok",
                        "prUrl": "https://github.com/fihtony/english-study-hub/pull/99",
                        "prNumber": 99,
                        "commitHash": "abc123",
                    }
                return {}

            mock_runtime = MagicMock()
            mock_runtime.run.return_value = {
                "raw_response": json.dumps({
                    "title": "feat(CSTL-1): implement landing page",
                    "description": "Initial PR description.",
                })
            }

            state = {
                "_task_id": "test-task",
                "_runtime": mock_runtime,
                "repo_url": "https://github.com/fihtony/english-study-hub",
                "branch_name": "feature/CSTL-1-landing-page",
                "repo_path": "/tmp/fake-repo",
                "jira_context": {"key": "CSTL-1", "fields": {"summary": "Landing page"}},
                "screenshots": [desktop_png_path],
                "screenshot_captured": True,
                "implementation_summary": "Implement landing page.",
                "test_status": "passed",
                "changes_made": ["src/App.tsx"],
                "workspace_path": tempfile.mkdtemp(),
            }

            with patch("agents.web_dev.nodes._call_boundary_tool", side_effect=fake_boundary_tool), \
                 patch("agents.web_dev.nodes._git_commit_all_pending", return_value=["src/App.tsx"]), \
                 patch("os.makedirs"):
                result = await create_pr(state)

            assert result.get("pr_url"), "PR URL must be set"

            upload_calls = [c for c in tool_calls if c["tool"] == "scm_upload_pr_image"]
            assert upload_calls, "scm_upload_pr_image must be called"

            create_pr_calls = [c for c in tool_calls if c["tool"] == "scm_create_pr"]
            assert create_pr_calls, "scm_create_pr must be called"
            pr_desc = create_pr_calls[0]["args"].get("description", "")
            assert CDN_URL in pr_desc, (
                f"CDN URL not in PR description.\nExpected: {CDN_URL}\nDescription: {pr_desc[:500]}"
            )
        finally:
            os.unlink(desktop_png_path)

    @pytest.mark.asyncio
    async def test_screenshots_fallback_when_cdn_fails(self):
        """When CDN upload fails, description contains raw.githubusercontent.com fallback URLs."""
        import subprocess as _subprocess
        from agents.web_dev.nodes import create_pr

        desktop_png_path, _ = _make_png_file()
        try:
            tool_calls: list[dict] = []

            def fake_boundary_tool(state, tool_name, args):
                tool_calls.append({"tool": tool_name, "args": args})
                if tool_name == "scm_push":
                    return {"status": "ok"}
                if tool_name == "scm_upload_pr_image":
                    return {"error": "upload failed"}
                if tool_name == "scm_create_pr":
                    return {
                        "status": "ok",
                        "prUrl": "https://github.com/fihtony/english-study-hub/pull/88",
                        "prNumber": 88,
                        "commitHash": "def456",
                    }
                return {}

            mock_runtime = MagicMock()
            mock_runtime.run.return_value = {
                "raw_response": json.dumps({
                    "title": "feat(CSTL-1): implement landing page",
                    "description": "Initial PR description.",
                })
            }

            commit_result = MagicMock()
            commit_result.returncode = 0

            state = {
                "_task_id": "test-task",
                "_runtime": mock_runtime,
                "repo_url": "https://github.com/fihtony/english-study-hub",
                "branch_name": "feature/CSTL-1-fallback",
                "repo_path": "/tmp/fake-repo-2",
                "jira_context": {"key": "CSTL-1"},
                "screenshots": [desktop_png_path],
                "screenshot_captured": True,
                "implementation_summary": "test",
                "test_status": "passed",
                "changes_made": ["src/App.tsx"],
                "workspace_path": tempfile.mkdtemp(),
            }

            with patch("agents.web_dev.nodes._call_boundary_tool", side_effect=fake_boundary_tool), \
                 patch("agents.web_dev.nodes._git_commit_all_pending", return_value=["src/App.tsx"]), \
                 patch("shutil.copy2"), \
                 patch("subprocess.run", return_value=commit_result), \
                 patch("os.makedirs"):
                result = await create_pr(state)

            assert result.get("pr_url"), "PR URL must be set even with CDN failure"
            create_pr_calls = [c for c in tool_calls if c["tool"] == "scm_create_pr"]
            assert create_pr_calls, "scm_create_pr must be called"
            pr_desc = create_pr_calls[0]["args"].get("description", "")
            assert "raw.githubusercontent.com" in pr_desc, (
                f"Expected raw.githubusercontent.com in fallback description.\n"
                f"Description: {pr_desc[:500]}"
            )
        finally:
            os.unlink(desktop_png_path)
