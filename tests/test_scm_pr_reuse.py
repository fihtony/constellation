import json
import unittest
from unittest.mock import patch

from scm.providers.github import GitHubProvider
from scm.providers.github_mcp import GitHubMCPProvider
import scm.app as scm_app


def _raw_pr(number: int, from_branch: str, to_branch: str, url: str) -> dict:
    return {
        "number": number,
        "title": "Existing PR",
        "body": "Already open",
        "state": "open",
        "html_url": url,
        "head": {
            "ref": from_branch,
            "repo": {"clone_url": "https://github.com/example/repo.git"},
        },
        "base": {"ref": to_branch},
        "user": {"login": "copilot"},
        "created_at": "2026-04-27T00:00:00Z",
    }


class _FakeGitHubProvider(GitHubProvider):
    def __init__(self):
        super().__init__(token="")

    def _request(self, method: str, path: str, payload: dict | None = None, timeout: int = 20):
        if method == "POST" and path == "repos/example/repo/pulls":
            return 422, {"message": "A pull request already exists for example:feature/demo."}
        if method == "GET" and path == "repos/example/repo/pulls?state=open&per_page=50":
            return 200, [_raw_pr(12, "feature/demo", "main", "https://github.com/example/repo/pull/12")]
        raise AssertionError(f"Unexpected request: {method} {path} {payload}")


class _FakeGitHubMCPProvider(GitHubMCPProvider):
    def __init__(self):
        super().__init__(token="")

    def _call(self, tool: str, args: dict, timeout: int = 60) -> dict:
        if tool == "create_pull_request":
            return {
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "A pull request already exists for feature/demo."}],
                }
            }
        if tool == "list_pull_requests":
            return {
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps([_raw_pr(34, "feature/demo", "main", "https://github.com/example/repo/pull/34")]),
                    }],
                }
            }
        raise AssertionError(f"Unexpected MCP call: {tool} {args}")


class SCMPrReuseTests(unittest.TestCase):
    def test_github_provider_reuses_existing_open_pr(self):
        provider = _FakeGitHubProvider()

        pr, status = provider.create_pr(
            "example",
            "repo",
            "feature/demo",
            "main",
            "Demo PR",
            "Body",
        )

        self.assertEqual(status, "already_exists")
        self.assertEqual(pr.get("htmlUrl"), "https://github.com/example/repo/pull/12")

    def test_github_mcp_provider_reuses_existing_open_pr(self):
        provider = _FakeGitHubMCPProvider()

        pr, status = provider.create_pr(
            "example",
            "repo",
            "feature/demo",
            "main",
            "Demo PR",
            "Body",
        )

        self.assertEqual(status, "already_exists")
        self.assertEqual(pr.get("htmlUrl"), "https://github.com/example/repo/pull/34")

    def test_scm_handler_treats_existing_pr_as_success(self):
        fake_pr = {
            "htmlUrl": "https://github.com/example/repo/pull/34",
            "fromBranch": "feature/demo",
            "toBranch": "main",
            "title": "Demo PR",
        }
        message = {
            "metadata": {
                "prPayload": {
                    "owner": "example",
                    "repo": "repo",
                    "fromBranch": "feature/demo",
                    "toBranch": "main",
                    "title": "Demo PR",
                    "description": "Body",
                }
            }
        }

        with patch.object(scm_app, "_provider") as mock_provider:
            mock_provider.create_pr.return_value = (fake_pr, "already_exists")
            status_text, artifacts = scm_app._handle_pr_create("", message)

        self.assertIn("PR already exists", status_text)
        self.assertEqual(len(artifacts), 1)
        artifact_payload = json.loads(artifacts[0]["parts"][0]["text"])
        self.assertEqual(artifact_payload["htmlUrl"], fake_pr["htmlUrl"])


if __name__ == "__main__":
    unittest.main()