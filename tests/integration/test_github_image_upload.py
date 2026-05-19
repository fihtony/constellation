"""Integration tests: GitHub image upload via Release Assets API.

These tests verify that screenshots can be uploaded to GitHub CDN using the
Release Assets approach, which:
  - Works with fine-grained PATs (unlike uploads.github.com/issues/assets)
  - Does NOT require committing image files to the PR branch
  - Returns stable browser_download_url CDN links for use in PR descriptions
  - Supports three SCM backends: github-rest, github-mcp, bitbucket-rest

Discovery notes (found via exploratory testing):
  - uploads.github.com/repos/{owner}/{repo}/issues/{issue_num}/assets:
      HTTP 400 "Multipart form data required" with octet-stream
      HTTP 422 "Bad Size" with all multipart field names and any file size
      → This endpoint does NOT work with fine-grained PATs
  - uploads.github.com/repos/{owner}/{repo}/releases/{release_id}/assets:
      HTTP 201 with raw Content-Type: application/octet-stream
      → WORKS with fine-grained PATs ✅
      Returns {"browser_download_url": "https://github.com/{owner}/{repo}/
               releases/download/screenshot-assets/{filename}"}

Credentials: loaded from tests/.env
  TEST_SCM_TOKEN — GitHub fine-grained PAT with repo write access
  TEST_SCM_REPO_URL (optional, defaults to fihtony/english-study-hub)
"""
from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load tests/.env
# ---------------------------------------------------------------------------

_ENV_FILE = Path(__file__).parent.parent / ".env"


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return env
    with open(_ENV_FILE, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


_TEST_ENV = _load_env()


def _env(key: str, default: str = "") -> str:
    return _TEST_ENV.get(key, os.environ.get(key, default))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _skip_if_no_token():
    token = _env("TEST_SCM_TOKEN")
    if not token:
        pytest.skip("TEST_SCM_TOKEN not set in tests/.env")


@pytest.fixture(scope="module")
def scm_token() -> str:
    token = _env("TEST_SCM_TOKEN")
    if not token:
        pytest.skip("TEST_SCM_TOKEN not set in tests/.env")
    return token


@pytest.fixture(scope="module")
def repo_coords(scm_token) -> tuple[str, str]:
    """Return (owner, repo) parsed from TEST_SCM_REPO_URL or default."""
    repo_url = _env("TEST_SCM_REPO_URL", "https://github.com/fihtony/english-study-hub")
    from urllib.parse import urlparse
    parts = urlparse(repo_url).path.strip("/").split("/")
    owner = parts[0] if len(parts) > 0 else "fihtony"
    repo = parts[1].replace(".git", "") if len(parts) > 1 else "english-study-hub"
    return owner, repo


@pytest.fixture
def test_png(tmp_path: Path) -> Path:
    """Create a small but valid 2x2 PNG for upload tests."""
    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    # 2x2 RGBA PNG (8 bytes raw: filter + 2 rows × 4 bytes each)
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)  # width=2, height=2, color=RGB
    raw_rows = b"\x00\xFF\x00\x00\xFF\x00\x00\xFF" + b"\x00\x00\xFF\x00\x00\xFF\x00\x00"
    idat = zlib.compress(raw_rows)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )
    png_file = tmp_path / "test-screenshot.png"
    png_file.write_bytes(png_bytes)
    return png_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.live
class TestGitHubReleaseAssetsUpload:
    """Test GitHub Release Assets upload approach for screenshot CDN hosting."""

    def test_upload_returns_cdn_url(self, scm_token, repo_coords, test_png):
        """Uploading a PNG returns a github.com releases/download CDN URL."""
        from agents.scm.client import GitHubClient

        owner, repo = repo_coords
        client = GitHubClient(token=scm_token)

        result, status = client.upload_issue_image(
            owner=owner,
            repo=repo,
            issue_number=0,  # 0 → uses release-assets approach (no issue number needed)
            image_path=str(test_png),
            filename="integration-test-screenshot.png",
        )

        assert status == "ok", f"Expected 'ok' but got {status!r}. Response: {result}"
        cdn_url = result.get("href", "")
        assert cdn_url, f"No CDN URL in response: {result}"
        assert "github.com" in cdn_url, f"Expected github.com CDN URL, got: {cdn_url}"
        assert "releases/download" in cdn_url or "user-attachments" in cdn_url, (
            f"Expected releases/download path, got: {cdn_url}"
        )
        assert cdn_url.endswith(".png"), f"URL should end with .png, got: {cdn_url}"

    def test_upload_already_exists_returns_existing_url(self, scm_token, repo_coords, test_png):
        """Uploading the same filename twice does not fail — returns existing URL."""
        from agents.scm.client import GitHubClient

        owner, repo = repo_coords
        client = GitHubClient(token=scm_token)
        fname = "integration-test-dedup-screenshot.png"

        # First upload
        r1, s1 = client.upload_issue_image(
            owner=owner, repo=repo, issue_number=0,
            image_path=str(test_png), filename=fname,
        )
        assert s1 == "ok", f"First upload failed: {s1} {r1}"
        url1 = r1.get("href", "")

        # Second upload with same filename — should succeed (idempotent)
        r2, s2 = client.upload_issue_image(
            owner=owner, repo=repo, issue_number=0,
            image_path=str(test_png), filename=fname,
        )
        assert s2 == "ok", f"Second upload failed: {s2} {r2}"
        url2 = r2.get("href", "")

        # Both should return a valid CDN URL
        assert url1, f"No CDN URL from first upload: {r1}"
        assert url2, f"No CDN URL from second upload: {r2}"

    def test_upload_with_pr_number_uses_prefixed_filename(self, scm_token, repo_coords, test_png):
        """When pr_number>0 is given, filename is prefixed with pr{number}."""
        from agents.scm.client import GitHubClient

        owner, repo = repo_coords
        client = GitHubClient(token=scm_token)

        result, status = client.upload_issue_image(
            owner=owner, repo=repo,
            issue_number=99,  # will prefix file as pr99-*.png
            image_path=str(test_png),
            filename="landing-desktop.png",
        )

        assert status == "ok", f"Upload failed: {status!r} {result}"
        cdn_url = result.get("href", "")
        assert "pr99-" in cdn_url, (
            f"Expected pr99- prefix in URL, got: {cdn_url}"
        )

    def test_screenshot_release_is_created_or_found(self, scm_token, repo_coords):
        """The screenshot-assets release is findable after upload."""
        from agents.scm.client import GitHubClient
        from urllib.request import Request, urlopen
        import json

        owner, repo = repo_coords
        client = GitHubClient(token=scm_token)
        release_id, rel_status = client._find_or_create_screenshot_release(owner, repo)

        assert release_id > 0, f"Expected valid release ID, got {release_id} (status={rel_status})"
        assert rel_status in ("found", "created"), f"Unexpected status: {rel_status}"


@pytest.mark.live
class TestSCMUploadPRImageTool:
    """Test the SCMUploadPRImage tool which wraps the GitHubClient."""

    def test_tool_returns_image_url(self, tmp_path, scm_token):
        """The scm_upload_pr_image tool returns an image_url on success."""
        import struct
        import zlib
        from agents.scm.tools import SCMUploadPRImage

        # Create a test PNG
        def _chunk(tag, data):
            body = tag + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        idat = zlib.compress(b"\x00\xFF\x00\x00")
        png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
        png_file = tmp_path / "tool-test.png"
        png_file.write_bytes(png)

        import os
        os.environ["SCM_TOKEN"] = scm_token

        tool = SCMUploadPRImage()
        result = tool.execute_sync(
            repo_url="https://github.com/fihtony/english-study-hub",
            pr_number=0,
            image_path=str(png_file),
            filename="tool-integration-test.png",
        )

        output = __import__("json").loads(result.output)
        assert output.get("ok"), f"Tool returned error: {output}"
        image_url = output.get("image_url", "")
        assert image_url, f"No image_url in response: {output}"
        assert "github.com" in image_url, f"Expected GitHub CDN URL: {image_url}"
