#!/usr/bin/env python3
"""Unit tests for the consolidated figma_client module.

Tests cover:
- URL parsing
- Rate limiting / throttling
- File-system cache (FigmaCache)
- Workspace cache helpers
- UI spec extraction
- Design token extraction
- Cached fetch methods (with mocked API)
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

# Ensure correct paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ui-design"))

os.environ.setdefault("FIGMA_MIN_CALL_INTERVAL_SECONDS", "0")
os.environ.setdefault("FIGMA_TOKEN", "test-token")

import figma_client


class TestParseUrl(unittest.TestCase):
    def test_design_url_with_node(self):
        url = "https://www.figma.com/design/mockFileKey123456/My-Design?node-id=1-470&t=abc"
        fk, nid = figma_client.parse_figma_url(url)
        self.assertEqual(fk, "mockFileKey123456")
        self.assertEqual(nid, "1:470")

    def test_file_url_with_node(self):
        url = "https://www.figma.com/file/abcDEF123/Test?node-id=5-100"
        fk, nid = figma_client.parse_figma_url(url)
        self.assertEqual(fk, "abcDEF123")
        self.assertEqual(nid, "5:100")

    def test_design_url_without_node(self):
        url = "https://www.figma.com/design/mockFileKey123456/My-Design"
        fk, nid = figma_client.parse_figma_url(url)
        self.assertEqual(fk, "mockFileKey123456")
        self.assertIsNone(nid)

    def test_focus_id_preferred_over_node_id(self):
        """In Figma dev-mode URLs, focus-id points to the section container."""
        url = (
            "https://www.figma.com/design/mockFocusFile987654/"
            "Demo-Design"
            "?node-id=2654-19645&focus-id=2654-19574&view=focus&m=dev"
        )
        fk, nid = figma_client.parse_figma_url(url)
        self.assertEqual(fk, "mockFocusFile987654")
        # focus-id should be preferred
        self.assertEqual(nid, "2654:19574")

    def test_node_id_used_when_no_focus_id(self):
        url = "https://www.figma.com/design/abc123/Test?node-id=10-20"
        fk, nid = figma_client.parse_figma_url(url)
        self.assertEqual(fk, "abc123")
        self.assertEqual(nid, "10:20")

    def test_invalid_url(self):
        url = "https://www.google.com/search?q=figma"
        fk, nid = figma_client.parse_figma_url(url)
        self.assertIsNone(fk)
        self.assertIsNone(nid)


class TestFigmaCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache = figma_client.FigmaCache(cache_dir=self.tmpdir, ttl=60)

    def test_put_and_get(self):
        data = {"name": "Test File", "version": "123"}
        self.cache.put("test-key", data)
        result = self.cache.get("test-key")
        self.assertEqual(result, data)

    def test_cache_miss(self):
        result = self.cache.get("nonexistent")
        self.assertIsNone(result)

    def test_cache_expiry(self):
        self.cache.ttl = 0  # expire immediately
        self.cache.put("expire-key", {"x": 1})
        time.sleep(0.1)
        result = self.cache.get("expire-key")
        self.assertIsNone(result)

    def test_clear(self):
        self.cache.put("k1", {"a": 1})
        self.cache.put("k2", {"b": 2})
        stats = self.cache.stats()
        self.assertEqual(stats["files"], 2)
        self.cache.clear()
        stats = self.cache.stats()
        self.assertEqual(stats["files"], 0)

    def test_stats(self):
        self.cache.put("k1", {"data": "test"})
        stats = self.cache.stats()
        self.assertEqual(stats["files"], 1)
        self.assertGreater(stats["total_bytes"], 0)


class TestWorkspaceCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_workspace_cache_filename(self):
        fn = figma_client.workspace_cache_filename("https://figma.com/file/abc", "Page1")
        self.assertTrue(fn.startswith("figma-data-"))
        self.assertTrue(fn.endswith(".json"))

    def test_save_and_load(self):
        data = {"fileMeta": {"name": "Test"}, "figmaUrl": "https://figma.com/file/abc"}
        fn = figma_client.workspace_cache_filename("https://figma.com/file/abc", "")
        figma_client.save_to_workspace(self.tmpdir, fn, data)
        loaded = figma_client.load_from_workspace(self.tmpdir, fn)
        self.assertEqual(loaded, data)

    def test_load_missing(self):
        result = figma_client.load_from_workspace(self.tmpdir, "nonexistent.json")
        self.assertIsNone(result)


class TestUISpecExtraction(unittest.TestCase):
    def test_extract_basic_node(self):
        node = {
            "type": "FRAME",
            "name": "Header",
            "absoluteBoundingBox": {"x": 0, "y": 0, "width": 1440, "height": 80},
            "fills": [{"type": "SOLID", "color": {"r": 1, "g": 0.5, "b": 0, "a": 1}}],
        }
        specs = figma_client.extract_ui_specs(node)
        self.assertEqual(specs["type"], "FRAME")
        self.assertEqual(specs["name"], "Header")
        self.assertEqual(specs["dimensions"]["width"], 1440)
        self.assertEqual(specs["dimensions"]["height"], 80)
        self.assertEqual(len(specs["colors"]["fills"]), 1)
        self.assertEqual(specs["colors"]["fills"][0]["hex"], "#FF7F00")

    def test_extract_typography(self):
        node = {
            "type": "TEXT",
            "name": "Title",
            "style": {
                "fontFamily": "Inter",
                "fontSize": 24,
                "fontWeight": 700,
                "lineHeightPx": 32,
            },
        }
        specs = figma_client.extract_ui_specs(node)
        self.assertEqual(specs["typography"]["fontFamily"], "Inter")
        self.assertEqual(specs["typography"]["fontSize"], 24)
        self.assertEqual(specs["typography"]["fontWeight"], 700)

    def test_extract_auto_layout(self):
        node = {
            "type": "FRAME",
            "name": "Card",
            "layoutMode": "VERTICAL",
            "paddingTop": 16,
            "paddingBottom": 16,
            "paddingLeft": 24,
            "paddingRight": 24,
            "itemSpacing": 8,
        }
        specs = figma_client.extract_ui_specs(node)
        self.assertEqual(specs["layout"]["mode"], "VERTICAL")
        self.assertEqual(specs["layout"]["paddingTop"], 16)
        self.assertEqual(specs["layout"]["itemSpacing"], 8)


class TestTraverseAndExtract(unittest.TestCase):
    def test_traverse_simple_tree(self):
        tree = {
            "type": "FRAME",
            "name": "Root",
            "children": [
                {"type": "TEXT", "name": "Title"},
                {"type": "RECTANGLE", "name": "Divider"},
                {
                    "type": "FRAME",
                    "name": "Content",
                    "children": [
                        {"type": "TEXT", "name": "Body"},
                    ],
                },
            ],
        }
        specs = figma_client.traverse_and_extract(tree, max_depth=3)
        names = [s["name"] for s in specs]
        self.assertIn("Root", names)
        self.assertIn("Title", names)
        self.assertIn("Body", names)
        self.assertEqual(len(specs), 5)  # Root + Title + Divider + Content + Body

    def test_max_depth_limit(self):
        deep = {"type": "FRAME", "name": "L0", "children": [
            {"type": "FRAME", "name": "L1", "children": [
                {"type": "FRAME", "name": "L2", "children": [
                    {"type": "TEXT", "name": "L3"},
                ]},
            ]},
        ]}
        specs = figma_client.traverse_and_extract(deep, max_depth=1)
        names = [s["name"] for s in specs]
        self.assertIn("L0", names)
        self.assertIn("L1", names)
        self.assertNotIn("L3", names)


class TestDesignTokens(unittest.TestCase):
    def test_extract_tokens(self):
        file_data = {
            "styles": {
                "s1": {"name": "Primary", "styleType": "FILL"},
                "s2": {"name": "Heading", "styleType": "TEXT"},
                "s3": {"name": "Shadow", "styleType": "EFFECT"},
            }
        }
        tokens = figma_client.extract_design_tokens(file_data)
        self.assertIn("Primary", tokens["colors"])
        self.assertIn("Heading", tokens["typography"])
        self.assertIn("Shadow", tokens["effects"])

    def test_no_styles(self):
        tokens = figma_client.extract_design_tokens({})
        self.assertEqual(tokens["colors"], {})


class TestRgbaToHex(unittest.TestCase):
    def test_red(self):
        self.assertEqual(figma_client._rgba_to_hex({"r": 1, "g": 0, "b": 0}), "#FF0000")

    def test_white(self):
        self.assertEqual(figma_client._rgba_to_hex({"r": 1, "g": 1, "b": 1}), "#FFFFFF")

    def test_black(self):
        self.assertEqual(figma_client._rgba_to_hex({"r": 0, "g": 0, "b": 0}), "#000000")


class TestCachedFetchMethods(unittest.TestCase):
    """Test cached fetch methods with mocked _figma_get."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cache = figma_client._cache
        figma_client._cache = figma_client.FigmaCache(cache_dir=self.tmpdir, ttl=60)

    def tearDown(self):
        figma_client._cache = self._orig_cache

    @patch("figma_client._figma_get")
    def test_fetch_file_meta_cached(self, mock_get):
        mock_get.return_value = (200, {
            "name": "Test", "lastModified": "2025-01-01", "thumbnailUrl": "http://...", "version": "1"
        })
        # First call — hits API
        meta, status = figma_client.fetch_file_meta_cached("abc123")
        self.assertEqual(status, "ok")
        self.assertEqual(meta["name"], "Test")
        self.assertEqual(mock_get.call_count, 1)

        # Second call — hits cache
        meta2, status2 = figma_client.fetch_file_meta_cached("abc123")
        self.assertEqual(status2, "ok")
        self.assertEqual(meta2["name"], "Test")
        self.assertEqual(mock_get.call_count, 1)  # no additional API call

    @patch("figma_client._figma_get")
    def test_fetch_nodes_cached(self, mock_get):
        mock_get.return_value = (200, {"nodes": {"1:470": {"document": {"type": "FRAME"}}}})
        result, status = figma_client.fetch_nodes_cached("abc123", ["1:470"])
        self.assertEqual(status, "ok")
        self.assertEqual(mock_get.call_count, 1)

        # Second call from cache
        result2, status2 = figma_client.fetch_nodes_cached("abc123", ["1:470"])
        self.assertEqual(status2, "ok")
        self.assertEqual(mock_get.call_count, 1)

    @patch("figma_client._figma_get")
    def test_fetch_pages_cached(self, mock_get):
        mock_get.return_value = (200, {
            "document": {"children": [
                {"id": "0:1", "name": "Page 1", "type": "CANVAS"},
                {"id": "0:2", "name": "Page 2", "type": "CANVAS"},
            ]}
        })
        pages, status = figma_client.fetch_pages_cached("abc123")
        self.assertEqual(status, "ok")
        self.assertEqual(len(pages), 2)
        self.assertEqual(mock_get.call_count, 1)

        pages2, status2 = figma_client.fetch_pages_cached("abc123")
        self.assertEqual(len(pages2), 2)
        self.assertEqual(mock_get.call_count, 1)


class TestRateLimiting(unittest.TestCase):
    """Test that the proactive rate limiter works."""

    def test_min_call_interval_env(self):
        with patch.dict(os.environ, {"FIGMA_MIN_CALL_INTERVAL_SECONDS": "5"}):
            self.assertEqual(figma_client._min_call_interval(), 5.0)

    def test_min_call_interval_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FIGMA_MIN_CALL_INTERVAL_SECONDS", None)
            val = figma_client._min_call_interval()
            # Default is 8 when env not set
            self.assertEqual(val, 8.0)


class TestToolLayerWrappers(unittest.TestCase):
    """Unit tests for list_pages(), fetch_page(), and fetch_node() — the
    tool-layer convenience wrappers used by provider_tools.py.
    """

    @patch("figma_client._figma_get")
    def test_list_pages_success(self, mock_get):
        mock_get.return_value = (200, {
            "document": {"children": [
                {"id": "0:1", "name": "Page 1", "type": "CANVAS"},
                {"id": "0:2", "name": "Page 2", "type": "CANVAS"},
            ]}
        })
        pages = figma_client.list_pages("abc123")
        self.assertIsInstance(pages, list)
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0]["name"], "Page 1")

    @patch("figma_client._figma_get")
    def test_list_pages_raises_on_error(self, mock_get):
        mock_get.return_value = (403, {"err": "forbidden"})
        with self.assertRaises(RuntimeError) as ctx:
            figma_client.list_pages("bad-key")
        self.assertIn("error_403", str(ctx.exception))

    @patch("figma_client._figma_get")
    def test_fetch_page_success(self, mock_get):
        # First call (fetch_pages), second call (fetch_nodes)
        mock_get.side_effect = [
            (200, {
                "document": {"children": [
                    {"id": "0:1", "name": "Home", "type": "CANVAS"},
                ]}
            }),
            (200, {"nodes": {"0:1": {"document": {"type": "CANVAS", "name": "Home"}}}}),
        ]
        result = figma_client.fetch_page("abc123", "Home")
        self.assertIsInstance(result, dict)
        self.assertIn("page", result)
        self.assertEqual(result["page"]["name"], "Home")

    @patch("figma_client._figma_get")
    def test_fetch_page_not_found_raises(self, mock_get):
        mock_get.return_value = (200, {
            "document": {"children": [
                {"id": "0:1", "name": "Home", "type": "CANVAS"},
            ]}
        })
        with self.assertRaises(RuntimeError) as ctx:
            figma_client.fetch_page("abc123", "NonExistentPageXYZ12345")
        self.assertIn("page_not_found", str(ctx.exception))

    @patch("figma_client._figma_get")
    def test_fetch_node_success(self, mock_get):
        mock_get.return_value = (200, {
            "nodes": {"1:470": {"document": {"type": "FRAME", "name": "Button"}}}
        })
        result = figma_client.fetch_node("abc123", "1:470")
        self.assertIsInstance(result, dict)
        self.assertIn("nodes", result)
        self.assertIn("1:470", result["nodes"])

    @patch("figma_client._figma_get")
    def test_fetch_node_raises_on_error(self, mock_get):
        mock_get.return_value = (404, {"err": "not found"})
        with self.assertRaises(RuntimeError) as ctx:
            figma_client.fetch_node("abc123", "9:999")
        self.assertIn("error_404", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
