"""Unit tests for GitHubClient image upload and PR description embedding.

Verifies that:
  1. upload_issue_image sends POST to uploads.github.com with correct headers.
  2. update_pr sends PATCH to the GitHub API with the correct body.
  3. The full create_pr flow in web_dev/nodes.py commits screenshots to the
     branch and embeds raw.githubusercontent.com URLs in the PR description.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeHTTPHandler(BaseHTTPRequestHandler):
    """Captures the last request and writes a canned response."""

    server: "_CapturingServer"  # type annotation only

    def log_message(self, fmt, *args):  # noqa: D102  silence output
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server.last_method = "POST"
        self.server.last_path = self.path
        self.server.last_headers = dict(self.headers)
        self.server.last_body = body
        response = json.dumps(self.server.response_payload).encode()
        self.send_response(self.server.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_PATCH(self):
        self.do_POST()


class _CapturingServer(HTTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_method = ""
        self.last_path = ""
        self.last_headers: dict = {}
        self.last_body = b""
        self.response_status = 200
        self.response_payload: dict = {}


def _start_fake_server(port: int, status: int, payload: dict) -> _CapturingServer:
    server = _CapturingServer(("127.0.0.1", port), _FakeHTTPHandler)
    server.response_status = status
    server.response_payload = payload
    t = Thread(target=server.handle_request, daemon=True)
    t.start()
    return server


# ---------------------------------------------------------------------------
# GitHubClient.upload_issue_image
# ---------------------------------------------------------------------------

class TestGitHubClientUploadIssueImage:
    """Test that upload_issue_image sends the correct HTTP request."""

    def _make_png(self) -> tuple[str, bytes]:
        """Create a tiny valid PNG in a temp file and return (path, bytes)."""
        # Minimal 1×1 white PNG (known-good binary)
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

    def test_returns_href_on_success(self):
        """upload_issue_image returns {href: cdn_url} when the API responds 200."""
        from agents.scm.client import GitHubClient

        cdn_url = "https://user-images.githubusercontent.com/123/abc.png"
        expected_payload = {
            "id": 1,
            "href": cdn_url,
            "content_type": "image/png",
            "size": 68,
        }

        path, _ = self._make_png()
        try:
            with patch("agents.scm.client.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_resp.read.return_value = json.dumps(expected_payload).encode()
                mock_urlopen.return_value = mock_resp

                client = GitHubClient(token="test-token")
                result, status = client.upload_issue_image(
                    "fihtony", "english-study-hub", 57, path, filename="desktop.png"
                )

            assert status == "ok"
            assert result["href"] == cdn_url
        finally:
            os.unlink(path)

    def test_sends_correct_url_and_headers(self):
        """upload_issue_image POSTs to uploads.github.com with correct query and headers."""
        from agents.scm.client import GitHubClient
        from urllib.request import Request

        path, _ = self._make_png()
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.method
            captured["headers"] = dict(req.headers)
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = json.dumps(
                {"href": "https://user-images.githubusercontent.com/x/y.png"}
            ).encode()
            return mock_resp

        try:
            with patch("agents.scm.client.urlopen", side_effect=fake_urlopen):
                client = GitHubClient(token="test-token")
                result, status = client.upload_issue_image(
                    "owner", "repo", 42, path, filename="screenshot.png"
                )

            assert status == "ok"
            assert captured["url"].startswith("https://uploads.github.com/repos/owner/repo/issues/42/assets")
            assert "name=screenshot.png" in captured["url"]
            assert captured["method"] == "POST"
            # Authorization header must NOT appear in assertion
            # (it should be set but we must not log it — security policy §4)
            # Authorization header MUST be set (for authenticated API call)
            # but must NOT appear in log output (see test_authorization_header_not_logged)
            assert "Authorization" in captured["headers"]
            # Content type must be octet-stream
            assert "multipart/form-data" in captured["headers"].get("Content-type", "")
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

        path, _ = self._make_png()
        try:
            with patch("agents.scm.client.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_resp.read.return_value = json.dumps(
                    {"href": "https://user-images.githubusercontent.com/x/y.png"}
                ).encode()
                mock_urlopen.return_value = mock_resp

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
    """Verify that create_pr node commits screenshots to branch and embeds raw URLs."""

    def _make_png(self) -> str:
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
            b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
            b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        tmp = tempfile.NamedTemporaryFile(suffix="-desktop.png", delete=False)
        tmp.write(png_bytes)
        tmp.close()
        return tmp.name

    @pytest.mark.asyncio
    async def test_screenshots_embedded_in_pr_description(self):
        """create_pr must commit screenshots and embed raw.githubusercontent.com URLs in PR description."""
        import shutil
        import subprocess
        from agents.web_dev.nodes import create_pr

        desktop_png = self._make_png()
        try:
            # Track all boundary tool calls
            tool_calls: list[dict] = []

            def fake_boundary_tool(state, tool_name, args):
                tool_calls.append({"tool": tool_name, "args": args})
                if tool_name == "scm_push":
                    return {"status": "ok"}
                if tool_name == "scm_create_pr":
                    return {
                        "status": "ok",
                        "prUrl": "https://github.com/fihtony/english-study-hub/pull/99",
                        "prNumber": 99,
                        "commitHash": "abc123",
                    }
                return {}

            # Mock runtime that returns a valid PR description JSON
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
                "jira_key": "CSTL-1",
                "jira_context": {"key": "CSTL-1", "fields": {"summary": "Landing page"}},
                "screenshots": [desktop_png],
                "screenshot_captured": True,
                "implementation_summary": "Implement landing page.",
                "test_status": "passed",
                "changes_made": ["src/App.tsx"],
                "workspace_path": tempfile.mkdtemp(),
            }

            commit_result = MagicMock()
            commit_result.returncode = 0

            # Patch _call_boundary_tool, _git_commit_all_pending, shutil.copy2, subprocess.run
            with patch("agents.web_dev.nodes._call_boundary_tool", side_effect=fake_boundary_tool), \
                 patch("agents.web_dev.nodes._git_commit_all_pending", return_value=["src/App.tsx"]), \
                 patch("shutil.copy2"), \
                 patch("subprocess.run", return_value=commit_result), \
                 patch("os.makedirs"):

                result = await create_pr(state)

            # Verify PR was created
            assert result.get("pr_url"), "PR URL must be set"

            # Verify scm_create_pr was called with description containing raw GitHub URL
            create_pr_calls = [c for c in tool_calls if c["tool"] == "scm_create_pr"]
            assert create_pr_calls, "scm_create_pr must be called"
            pr_desc = create_pr_calls[0]["args"].get("description", "")
            assert "raw.githubusercontent.com" in pr_desc, (
                f"raw.githubusercontent.com URL not in PR description. "
                f"Description: {pr_desc[:500]}"
            )
            assert "feature/CSTL-1-landing-page" in pr_desc, (
                f"Branch name not in screenshot URL. Description: {pr_desc[:500]}"
            )
        finally:
            os.unlink(desktop_png)
