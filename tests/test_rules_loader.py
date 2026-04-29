#!/usr/bin/env python3
"""Focused tests for shared rule and skill prompt loading."""

from __future__ import annotations

import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class SkillLoaderTests(unittest.TestCase):
    def test_load_skills_reads_workspace_skill_guides(self):
        from common.rules_loader import load_skills

        combined = load_skills(
            [
                "constellation-architecture-delivery",
                "constellation-frontend-delivery",
            ]
        )

        self.assertIn("Architecture Delivery", combined)
        self.assertIn("Frontend Delivery", combined)

    def test_build_system_prompt_includes_skills_section(self):
        from common.rules_loader import build_system_prompt

        prompt = build_system_prompt(
            "Base system prompt.",
            "web",
            skill_names=["constellation-backend-delivery"],
        )

        self.assertIn("Base system prompt.", prompt)
        self.assertIn("ADDITIONAL SKILLS", prompt)
        self.assertIn("Backend Delivery", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)