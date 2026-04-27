#!/usr/bin/env python3
"""Offline unit tests for the Stitch client parsing helpers."""

from __future__ import annotations

import importlib.util
import os
import unittest
from unittest.mock import patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STITCH_CLIENT_PATH = os.path.join(PROJECT_ROOT, "ui-design", "stitch_client.py")


def _load_stitch_client_module():
    spec = importlib.util.spec_from_file_location("stitch_client_under_test", STITCH_CLIENT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load stitch client from {STITCH_CLIENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StitchClientTests(unittest.TestCase):
    def setUp(self):
        self.stitch_client = _load_stitch_client_module()

    def test_list_screens_parses_json_list(self):
        with patch.object(
            self.stitch_client,
            "_stitch_post",
            return_value=(
                200,
                {
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": '[{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"Landing Page"}]',
                            }
                        ]
                    }
                },
            ),
        ):
            screens, status = self.stitch_client.list_screens("123")
        self.assertEqual(status, "ok")
        self.assertEqual(screens[0]["name"], "Landing Page")

    def test_list_screens_parses_plain_text_pairs(self):
        with patch.object(
            self.stitch_client,
            "_stitch_post",
            return_value=(
                200,
                {
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": 'id: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  name: Lesson Library',
                            }
                        ]
                    }
                },
            ),
        ):
            screens, status = self.stitch_client.list_screens("123")
        self.assertEqual(status, "ok")
        self.assertEqual(screens[0]["id"], "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    def test_find_screen_by_name_supports_exact_and_partial_matches(self):
        screens = [
            {"id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "Landing Page"},
            {"id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "name": "Lesson Library"},
        ]
        with patch.object(self.stitch_client, "list_screens", return_value=(screens, "ok")):
            found_exact, status_exact = self.stitch_client.find_screen_by_name("123", "landing page")
            found_partial, status_partial = self.stitch_client.find_screen_by_name("123", "Lesson")
        self.assertEqual(status_exact, "ok")
        self.assertEqual(found_exact["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(status_partial, "ok")
        self.assertEqual(found_partial["id"], "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")


if __name__ == "__main__":
    unittest.main(verbosity=2)