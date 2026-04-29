"""Figma REST API client — read-only design data fetching."""

from __future__ import annotations

import json
import os
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

FIGMA_API_BASE = "https://api.figma.com/v1"

# Maximum number of retries when Figma returns 429 (rate-limited).
_MAX_RETRIES = 3
# Base back-off in seconds (doubles on each retry).
_BACKOFF_BASE = 2.0


def _figma_token():
    return os.environ.get("FIGMA_TOKEN", "").strip()


def _max_retry_wait_seconds() -> float:
    raw = os.environ.get("FIGMA_MAX_RETRY_WAIT_SECONDS", "5").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


def _corp_ca_bundle():
    return (
        os.environ.get("CORP_CA_BUNDLE", "") or os.environ.get("SSL_CERT_FILE", "")
    )


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ca_bundle = _corp_ca_bundle()
    if ca_bundle and os.path.isfile(ca_bundle):
        ctx.load_verify_locations(ca_bundle)
    return ctx


def _figma_get(path):
    """Make an authenticated GET to the Figma REST API. Returns (status, body).

    Automatically retries on HTTP 429 (rate-limited) using the Retry-After
    header when present, falling back to exponential back-off.
    """
    url = f"{FIGMA_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    headers = {}
    token = _figma_token()
    if token:
        headers["X-Figma-Token"] = token
    request = Request(url, headers=headers, method="GET")
    last_status, last_body = 0, {}
    for attempt in range(_MAX_RETRIES + 1):
        try:
            with urlopen(request, timeout=30, context=_ssl_ctx()) as resp:
                raw = resp.read().decode("utf-8")
                body = json.loads(raw) if raw.strip() else {}
                return resp.status, body
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except Exception:
                body = {"error": raw[:500]}
            last_status, last_body = exc.code, body
            if exc.code == 429 and attempt < _MAX_RETRIES:
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    requested_wait = float(retry_after) if retry_after else _BACKOFF_BASE * (2 ** attempt)
                except ValueError:
                    requested_wait = _BACKOFF_BASE * (2 ** attempt)
                wait_cap = _max_retry_wait_seconds()
                wait = min(requested_wait, wait_cap) if wait_cap > 0 else 0.0
                print(
                    f"[figma_client] 429 rate-limited; waiting {wait:.1f}s "
                    f"(requested {requested_wait:.1f}s) "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES})",
                    flush=True,
                )
                if wait > 0:
                    time.sleep(wait)
                continue
            return exc.code, body
        except URLError as exc:
            return 0, {"error": str(exc.reason)}
    return last_status, last_body


def parse_figma_url(url):
    """
    Parse a Figma URL and extract file_key and optional node_id.
    Supports:
      https://www.figma.com/design/{file_key}/{name}?node-id={node_id}
      https://www.figma.com/file/{file_key}/{name}?node-id={node_id}
    Returns (file_key, node_id or None).
    """
    parsed = urlparse(url)
    # Path like /design/FILE_KEY/... or /file/FILE_KEY/...
    m = re.match(r"^/(?:design|file)/([^/]+)", parsed.path)
    if not m:
        return None, None
    file_key = m.group(1)
    # node-id from query string
    from urllib.parse import parse_qs
    qs = parse_qs(parsed.query)
    node_id = None
    if "node-id" in qs:
        # Figma uses dash-separated node IDs in URLs, colon-separated in API
        node_id = qs["node-id"][0].replace("-", ":")
    return file_key, node_id


def fetch_file_meta(file_key):
    """Fetch high-level file metadata (name, last modified, thumbnailUrl)."""
    status, body = _figma_get(f"files/{file_key}?depth=1")
    if status == 200:
        return {
            "name": body.get("name"),
            "lastModified": body.get("lastModified"),
            "thumbnailUrl": body.get("thumbnailUrl"),
            "version": body.get("version"),
            "status": "ok",
        }, "ok"
    return body, f"error_{status}"


def fetch_nodes(file_key, node_ids):
    """
    Fetch specific nodes by ID list.
    node_ids: list of colon-separated IDs, e.g. ["569:1429"]
    """
    ids_param = ",".join(node_ids)
    status, body = _figma_get(f"files/{file_key}/nodes?ids={ids_param}")
    if status == 200:
        return body, "ok"
    return body, f"error_{status}"


def fetch_from_url(figma_url):
    """
    Convenience function: parse a Figma URL and fetch relevant data.
    Returns a summary dict with file metadata and optional node data.
    """
    file_key, node_id = parse_figma_url(figma_url)
    if not file_key:
        return {"error": "could_not_parse_figma_url", "url": figma_url}, "parse_error"

    result = {"fileKey": file_key, "sourceUrl": figma_url}

    meta, meta_status = fetch_file_meta(file_key)
    result["fileMeta"] = meta
    result["fileMetaStatus"] = meta_status

    if node_id and meta_status == "ok":
        nodes, nodes_status = fetch_nodes(file_key, [node_id])
        result["nodeId"] = node_id
        result["nodes"] = nodes
        result["nodesStatus"] = nodes_status

    result["status"] = "ok" if meta_status == "ok" else meta_status
    return result, result["status"]


# ---------------------------------------------------------------------------
# Page-level helpers
# ---------------------------------------------------------------------------

def fetch_pages(file_key):
    """
    Fetch the list of top-level pages (CANVAS nodes) in a Figma file.
    Returns (pages_list, status) where pages_list is
    [{"id": "...", "name": "..."}, ...].
    """
    status, body = _figma_get(f"files/{file_key}?depth=1")
    if status != 200:
        return [], f"error_{status}"
    doc = body.get("document", {}) if isinstance(body, dict) else {}
    children = doc.get("children", []) if isinstance(doc, dict) else []
    pages = [
        {"id": child["id"], "name": child["name"]}
        for child in children
        if child.get("type") == "CANVAS"
    ]
    return pages, "ok"


def fetch_page_by_name(file_key, page_name):
    """
    Fetch a specific page's node data by page name (case-insensitive, fuzzy).

    Returns (result_dict, status) where result_dict contains:
      - "page": {"id": ..., "name": ...}
      - "nodes": raw nodes API response
      - "nodesStatus": "ok" or error
      - "availablePages": list of all page names (for debugging)

    Falls back to the closest fuzzy match when an exact match is not found.
    """
    pages, page_status = fetch_pages(file_key)
    if page_status != "ok":
        return {"error": "could_not_fetch_pages", "detail": page_status}, page_status

    target = page_name.strip().lower()

    def _score(page):
        name_lower = page["name"].lower()
        if name_lower == target:
            return 1000
        target_tokens = set(re.split(r"[\s\-_/]+", target))
        name_tokens = set(re.split(r"[\s\-_/]+", name_lower))
        return len(target_tokens & name_tokens)

    best = max(pages, key=_score, default=None)
    if best is None:
        return {
            "error": "no_pages_found",
            "availablePages": [],
        }, "no_pages_found"

    if _score(best) == 0:
        return {
            "error": "page_not_found",
            "query": page_name,
            "availablePages": [p["name"] for p in pages],
        }, "page_not_found"

    nodes, nodes_status = fetch_nodes(file_key, [best["id"]])
    return {
        "page": best,
        "nodes": nodes,
        "nodesStatus": nodes_status,
        "availablePages": [p["name"] for p in pages],
    }, "ok" if nodes_status == "ok" else nodes_status
