from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from scm import app as scm_app


class SCMCloneAuthTests(unittest.TestCase):
    def test_github_clone_uses_basic_auth_header_without_tokenized_url(self):
        with patch.object(scm_app, "_SCM_TOKEN", "example-scm-token"), patch.object(scm_app, "_CORP_CA_BUNDLE", ""):
            clone_url, git_config = scm_app._resolve_clone_auth(
                "example-org",
                "example-app",
                "https://github.com/example-org/example-app.git",
            )

        expected_header = base64.b64encode(b"x-access-token:example-scm-token").decode("ascii")
        self.assertEqual(clone_url, "https://github.com/example-org/example-app.git")
        self.assertEqual(
            git_config,
            ["-c", "credential.helper=", "-c", f"http.extraHeader=AUTHORIZATION: basic {expected_header}"],
        )

    def test_non_github_clone_keeps_bearer_header_auth(self):
        with patch.object(scm_app, "_SCM_TOKEN", "example-scm-token"), patch.object(scm_app, "_CORP_CA_BUNDLE", ""):
            clone_url, git_config = scm_app._resolve_clone_auth(
                "proj",
                "repo",
                "https://bitbucket.example.com/scm/proj/repo.git",
            )

        self.assertEqual(clone_url, "https://bitbucket.example.com/scm/proj/repo.git")
        self.assertEqual(git_config, ["-c", "credential.helper=", "-c", "http.extraHeader=Authorization: Bearer example-scm-token"])

    def test_existing_tokenized_clone_url_is_sanitized_and_uses_header(self):
        authed_url = "https://x-access-token:example-scm-token@github.com/example-org/example-app.git"
        with patch.object(scm_app, "_SCM_TOKEN", "example-scm-token"), patch.object(scm_app, "_CORP_CA_BUNDLE", ""):
            clone_url, git_config = scm_app._resolve_clone_auth(
                "example-org",
                "example-app",
                authed_url,
            )

        expected_header = base64.b64encode(b"x-access-token:example-scm-token").decode("ascii")
        self.assertEqual(clone_url, "https://github.com/example-org/example-app.git")
        self.assertEqual(
            git_config,
            ["-c", "credential.helper=", "-c", f"http.extraHeader=AUTHORIZATION: basic {expected_header}"],
        )


if __name__ == "__main__":
    unittest.main()