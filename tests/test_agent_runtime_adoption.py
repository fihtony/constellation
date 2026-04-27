#!/usr/bin/env python3
"""Static guardrails ensuring agent files use the unified runtime abstraction."""

from __future__ import annotations

import os
import unittest
from pathlib import Path


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_FILES = [
    os.path.join(PROJECT_ROOT, "team-lead", "app.py"),
    os.path.join(PROJECT_ROOT, "web", "app.py"),
    os.path.join(PROJECT_ROOT, "jira", "app.py"),
    os.path.join(PROJECT_ROOT, "scm", "app.py"),
    os.path.join(PROJECT_ROOT, "ui-design", "app.py"),
]


class AgentRuntimeAdoptionTests(unittest.TestCase):
    def test_agents_no_longer_import_generate_text_directly(self):
        for path in AGENT_FILES:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertNotIn("from common.llm_client import generate_text", content)

    def test_agents_import_runtime_adapter(self):
        for path in AGENT_FILES:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("from common.runtime.adapter import get_runtime", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)