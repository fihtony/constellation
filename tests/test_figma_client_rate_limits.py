from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UI_DESIGN_DIR = PROJECT_ROOT / "ui-design"
if str(UI_DESIGN_DIR) not in sys.path:
    sys.path.insert(0, str(UI_DESIGN_DIR))

FIGMA_CLIENT_SPEC = importlib.util.spec_from_file_location("figma_client", UI_DESIGN_DIR / "figma_client.py")
figma_client = importlib.util.module_from_spec(FIGMA_CLIENT_SPEC)
assert FIGMA_CLIENT_SPEC and FIGMA_CLIENT_SPEC.loader
FIGMA_CLIENT_SPEC.loader.exec_module(figma_client)


class FigmaClientRateLimitTests(unittest.TestCase):
    def test_figma_get_caps_large_retry_after(self):
        sleep_calls: list[float] = []

        def fake_urlopen(*_args, **_kwargs):
            raise HTTPError(
                url="https://api.figma.com/v1/files/test-file?depth=1",
                code=429,
                msg="Too Many Requests",
                hdrs={"Retry-After": "72780"},
                fp=io.BytesIO(b'{"error":"rate_limited"}'),
            )

        with mock.patch.object(figma_client, "_ssl_ctx", return_value=None), mock.patch.object(
            figma_client,
            "urlopen",
            side_effect=fake_urlopen,
        ), mock.patch.object(figma_client, "_max_retry_wait_seconds", return_value=5.0), mock.patch.object(
            figma_client.time,
            "sleep",
            side_effect=lambda seconds: sleep_calls.append(seconds),
        ):
            status, body = figma_client._figma_get("files/test-file?depth=1")

        self.assertEqual(status, 429)
        self.assertEqual(body.get("error"), "rate_limited")
        # _MAX_RETRIES=5 means 5 retry waits before giving up on the 6th attempt.
        self.assertEqual(sleep_calls, [5.0, 5.0, 5.0, 5.0, 5.0])


if __name__ == "__main__":
    unittest.main(verbosity=2)