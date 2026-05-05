#!/usr/bin/env python3
"""Focused regression tests for E2E SCM URL parsing helpers."""

from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_e2e as e2e


class ParseScmTargetTests(unittest.TestCase):
    def test_parse_github_repo_url(self):
        target = e2e._parse_scm_target("https://github.com/example-org/example-app.git")

        self.assertEqual(target["provider"], "github")
        self.assertEqual(target["owner"], "example-org")
        self.assertEqual(target["repo"], "example-app")
        self.assertEqual(target["base_url"], "https://github.com")

    def test_parse_bitbucket_browse_url(self):
        target = e2e._parse_scm_target(
            "https://bitbucket.example.com/projects/EMF/repos/mobile-web/browse"
        )

        self.assertEqual(target["provider"], "bitbucket")
        self.assertEqual(target["owner"], "EMF")
        self.assertEqual(target["repo"], "mobile-web")
        self.assertEqual(target["base_url"], "https://bitbucket.example.com")


if __name__ == "__main__":
    unittest.main()