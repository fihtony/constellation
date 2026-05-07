from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from common.artifact_store import ArtifactStore


class ArtifactStoreTests(unittest.TestCase):
    def test_falls_back_to_temp_root_when_configured_root_is_unwritable(self):
        with tempfile.TemporaryDirectory(prefix="artifact_store_") as temp_dir:
            configured_root = os.path.join(temp_dir, "blocked", "compass-123")
            expected_fallback = os.path.join(temp_dir, "constellation-artifacts", "compass-123")
            mkdir_calls = []

            def fake_makedirs(path, exist_ok=False):
                mkdir_calls.append(path)
                if os.path.realpath(path) == os.path.realpath(configured_root):
                    raise PermissionError("blocked")

            with patch("common.artifact_store.tempfile.gettempdir", return_value=temp_dir), patch(
                "common.artifact_store.os.makedirs",
                side_effect=fake_makedirs,
            ):
                store = ArtifactStore(root=configured_root)

        self.assertEqual(store.root, expected_fallback)
        self.assertEqual(mkdir_calls[0], configured_root)
        self.assertEqual(mkdir_calls[1], expected_fallback)


if __name__ == "__main__":
    unittest.main()