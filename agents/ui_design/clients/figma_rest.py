"""Figma REST API v1 read-only client.

Stdlib-only.  Rate limiting is respected via a configurable minimum
inter-call interval (default: 8 s, matching Figma's ~10 req/min Tier-1
budget for Dev seats).  Retry on 429 with exponential back-off.

Figma REST API docs: https://www.figma.com/developers/api
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

_FIGMA_API_BASE = "https://api.figma.com/v1"
_FILE_KEY_RE = re.compile(
    r"figma\.com/(?:design|file)/([a-zA-Z0-9_-]+)", re.IGNORECASE
)
_RATE_LOCK = threading.Lock()
_LAST_CALL_TIME: float = 0.0


def _parse_file_key(url_or_key: str) -> str:
    """Extract Figma file key from a full URL or return as-is."""
    m = _FILE_KEY_RE.search(url_or_key)
    return m.group(1) if m else url_or_key


def _throttle(min_interval: float) -> None:
    """Block until at least *min_interval* seconds have passed since last call."""
    global _LAST_CALL_TIME
    if min_interval <= 0:
        return
    with _RATE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_CALL_TIME
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        _LAST_CALL_TIME = time.monotonic()


class FigmaClient:
    """Figma REST API v1 read-only client.

    Parameters
    ----------
    token:
        Figma personal access token.
    min_call_interval:
        Minimum seconds between consecutive API calls (rate limiting).
        Set to 0 to disable.  Defaults to 8 s.
    """

    def __init__(
        self,
        token: str,
        min_call_interval: float | None = None,
    ) -> None:
        self._token = token.strip()
        if min_call_interval is None:
            try:
                min_call_interval = float(
                    os.environ.get("FIGMA_MIN_CALL_INTERVAL_SECONDS", "8")
                )
            except ValueError:
                min_call_interval = 8.0
        self._min_interval = max(0.0, min_call_interval)

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def get_file(
        self,
        url_or_key: str,
        timeout: int = 60,
    ) -> tuple[dict, str]:
        """Fetch Figma file metadata (pages, components, styles).

        Returns (file_dict, status).  ``file_dict`` contains at minimum:
        ``name``, ``lastModified``, ``document.children`` (pages).
        """
        file_key = _parse_file_key(url_or_key)
        try:
            data = self._get(f"/files/{file_key}", timeout=timeout)
            return data, "ok"
        except HTTPError as exc:
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    def list_pages(
        self, url_or_key: str, timeout: int = 60
    ) -> tuple[list[dict], str]:
        """Return a list of pages ``[{id, name}, ...]`` in the file."""
        data, status = self.get_file(url_or_key, timeout=timeout)
        if not data:
            return [], status
        pages = [
            {"id": child.get("id"), "name": child.get("name")}
            for child in (
                data.get("document", {}).get("children", [])
            )
        ]
        return pages, "ok"

    def get_node(
        self,
        url_or_key: str,
        node_id: str,
        timeout: int = 60,
    ) -> tuple[dict, str]:
        """Fetch a specific node (frame / component) from the file."""
        file_key = _parse_file_key(url_or_key)
        try:
            data = self._get(
                f"/files/{file_key}/nodes?ids={node_id}", timeout=timeout
            )
            nodes = data.get("nodes", {})
            node_data = nodes.get(node_id, {})
            return node_data, "ok"
        except HTTPError as exc:
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    def get_file_styles(
        self, url_or_key: str, timeout: int = 60
    ) -> tuple[dict, str]:
        """Fetch design tokens (styles) defined in the file."""
        file_key = _parse_file_key(url_or_key)
        try:
            data = self._get(f"/files/{file_key}/styles", timeout=timeout)
            return data, "ok"
        except HTTPError as exc:
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    @staticmethod
    def parse_file_key(url_or_key: str) -> str:
        return _parse_file_key(url_or_key)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get(self, path: str, timeout: int = 60) -> dict:
        _throttle(self._min_interval)
        url = f"{_FIGMA_API_BASE}{path}"
        headers = {
            "X-Figma-Token": self._token,
            "Accept": "application/json",
        }
        req = Request(url, headers=headers, method="GET")
        max_retries = 3
        backoff = 8.0
        for attempt in range(max_retries):
            try:
                with urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw)
            except HTTPError as exc:
                if exc.code == 429 and attempt < max_retries - 1:
                    wait = backoff * (2 ** attempt)
                    print(f"[figma-client] 429 rate-limited, retrying in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                raise
