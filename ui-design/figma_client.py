"""Figma REST API client — read-only design data fetching with caching and rate limiting.

Consolidated client that handles:
- REST API calls with proactive rate limiting and 429 retry
- File-system cache to avoid redundant API calls
- UI spec extraction (colors, typography, layout)
- Design token extraction from file styles
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

FIGMA_API_BASE = "https://api.figma.com/v1"

# Maximum number of retries when Figma returns 429 (rate-limited).
_MAX_RETRIES = 5
# Base back-off in seconds (doubles on each retry).
_BACKOFF_BASE = 8.0

# Proactive rate limiter: enforce minimum interval between consecutive Figma API calls.
# Figma grants Dev/Full seats 10-20 calls/min for Tier-1 endpoints.
# An 8-second interval (≈7.5 calls/min) stays safely within that budget.
_RATE_LIMIT_LOCK = threading.Lock()
_LAST_FIGMA_CALL_TIME: float = 0.0


def _figma_token():
    return os.environ.get("FIGMA_TOKEN", "").strip()


def _min_call_interval() -> float:
    """Minimum seconds between consecutive Figma API calls (configurable via env)."""
    raw = os.environ.get("FIGMA_MIN_CALL_INTERVAL_SECONDS", "8").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 8.0


def _max_retry_wait_seconds() -> float:
    """Maximum seconds to wait on a 429 retry (0 = no cap; default uncapped)."""
    raw = os.environ.get("FIGMA_MAX_RETRY_WAIT_SECONDS", "0").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _throttle_figma_call() -> None:
    """Block until the minimum inter-call interval has elapsed."""
    global _LAST_FIGMA_CALL_TIME
    interval = _min_call_interval()
    if interval <= 0:
        return
    with _RATE_LIMIT_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_FIGMA_CALL_TIME
        if elapsed < interval:
            wait_time = interval - elapsed
            print(
                f"[figma_client] Proactive rate-limit: waiting {wait_time:.1f}s "
                f"(interval={interval:.0f}s)",
                flush=True,
            )
            time.sleep(wait_time)
        _LAST_FIGMA_CALL_TIME = time.monotonic()


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

    Proactively throttles requests to stay within Figma's rate limits, and
    automatically retries on HTTP 429 using the Retry-After header.
    """
    _throttle_figma_call()

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
                # Honour the full Retry-After value; only apply cap when explicitly configured.
                wait_cap = _max_retry_wait_seconds()
                wait = min(requested_wait, wait_cap) if wait_cap > 0 else requested_wait
                print(
                    f"[figma_client] 429 rate-limited; waiting {wait:.1f}s "
                    f"(Retry-After={requested_wait:.1f}s) "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES})",
                    flush=True,
                )
                if wait > 0:
                    time.sleep(wait)
                # After a 429 retry-wait, reset the throttle clock so the next
                # proactive check does not add an extra delay on top.
                with _RATE_LIMIT_LOCK:
                    global _LAST_FIGMA_CALL_TIME
                    _LAST_FIGMA_CALL_TIME = time.monotonic()
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
    When a focus-id parameter is present (common in Figma dev-mode URLs),
    it is preferred over node-id because it typically points to the
    section/frame container rather than a nested sub-component.
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
    # Prefer focus-id (section/frame container) over node-id (sub-component)
    if "focus-id" in qs:
        node_id = qs["focus-id"][0].replace("-", ":")
    elif "node-id" in qs:
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


# ---------------------------------------------------------------------------
# File-system cache
# ---------------------------------------------------------------------------

class FigmaCache:
    """File-based cache for Figma API responses to avoid redundant calls."""

    def __init__(self, cache_dir: str = "", ttl: int = 3600):
        if not cache_dir:
            cache_dir = os.environ.get("FIGMA_CACHE_DIR", "/tmp/figma_cache")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl

    def _cache_file(self, key: str) -> Path:
        slug = hashlib.md5(key.encode()).hexdigest()
        return self.cache_dir / f"{slug}.json"

    def get(self, key: str) -> dict | None:
        path = self._cache_file(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self.ttl:
            path.unlink(missing_ok=True)
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def put(self, key: str, data: dict) -> None:
        path = self._cache_file(key)
        try:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            print(f"[figma_client] cache write error: {exc}")

    def clear(self) -> None:
        for f in self.cache_dir.glob("*.json"):
            f.unlink(missing_ok=True)

    def stats(self) -> dict:
        files = list(self.cache_dir.glob("*.json"))
        total = sum(f.stat().st_size for f in files)
        return {"files": len(files), "total_bytes": total, "dir": str(self.cache_dir)}


# Module-level singleton cache
_cache = FigmaCache()


def get_cache() -> FigmaCache:
    """Return the module-level cache instance."""
    return _cache


# ---------------------------------------------------------------------------
# Cached fetch helpers
# ---------------------------------------------------------------------------

def fetch_file_meta_cached(file_key: str) -> tuple[dict, str]:
    """Fetch file metadata with cache."""
    cache_key = f"meta:{file_key}"
    cached = _cache.get(cache_key)
    if cached:
        print(f"[figma_client] cache HIT: file meta {file_key}")
        return cached, "ok"
    meta, status = fetch_file_meta(file_key)
    if status == "ok":
        _cache.put(cache_key, meta)
    return meta, status


def fetch_nodes_cached(file_key: str, node_ids: list[str]) -> tuple[dict, str]:
    """Fetch nodes with cache."""
    cache_key = f"nodes:{file_key}:{','.join(sorted(node_ids))}"
    cached = _cache.get(cache_key)
    if cached:
        print(f"[figma_client] cache HIT: nodes {node_ids}")
        return cached, "ok"
    result, status = fetch_nodes(file_key, node_ids)
    if status == "ok":
        _cache.put(cache_key, result)
    return result, status


def fetch_pages_cached(file_key: str) -> tuple[list, str]:
    """Fetch pages list with cache."""
    cache_key = f"pages:{file_key}"
    cached = _cache.get(cache_key)
    if cached:
        print(f"[figma_client] cache HIT: pages {file_key}")
        return cached, "ok"
    pages, status = fetch_pages(file_key)
    if status == "ok":
        _cache.put(cache_key, pages)
    return pages, status


def fetch_from_url_cached(figma_url: str) -> tuple[dict, str]:
    """Convenience: parse URL and fetch with cache (meta + optional node)."""
    cache_key = f"url:{figma_url}"
    cached = _cache.get(cache_key)
    if cached:
        print(f"[figma_client] cache HIT: url fetch")
        return cached, cached.get("status", "ok")
    result, status = fetch_from_url(figma_url)
    if status == "ok":
        _cache.put(cache_key, result)
    return result, status


# ---------------------------------------------------------------------------
# Bulk download — fetch entire file tree in a single API call
# ---------------------------------------------------------------------------

def fetch_full_file(file_key: str, use_cache: bool = True) -> tuple[dict, str]:
    """Fetch the complete Figma file tree in ONE API call.

    This is the most efficient approach: a single GET /files/{key} returns
    ALL pages and ALL nodes.  The response can be large (tens of MB for big files)
    but avoids per-page / per-node calls that trigger rate limits.

    Returns (file_data, status).
    """
    cache_key = f"full_file:{file_key}"
    if use_cache:
        cached = _cache.get(cache_key)
        if cached:
            print(f"[figma_client] cache HIT: full file {file_key}")
            return cached, "ok"

    status_code, body = _figma_get(f"files/{file_key}")
    if status_code == 200:
        if use_cache:
            _cache.put(cache_key, body)
        return body, "ok"
    return body, f"error_{status_code}"


# ---------------------------------------------------------------------------
# Workspace cache helpers (save/load cached data into shared workspace)
# ---------------------------------------------------------------------------

def save_to_workspace(workspace_path: str, filename: str, data: dict) -> str:
    """Save Figma data to the shared workspace for downstream agents.

    Returns the full path of the saved file.
    """
    dest_dir = os.path.join(workspace_path, "ui-design")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return dest


def load_from_workspace(workspace_path: str, filename: str) -> dict | None:
    """Load previously saved Figma data from the shared workspace."""
    path = os.path.join(workspace_path, "ui-design", filename)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return None


def workspace_cache_filename(figma_url: str, page_name: str = "") -> str:
    """Deterministic filename for workspace-cached Figma data."""
    key = f"{figma_url}||{page_name}"
    slug = hashlib.md5(key.encode()).hexdigest()[:8]
    return f"figma-data-{slug}.json"


# ---------------------------------------------------------------------------
# UI spec extraction (from enhanced client)
# ---------------------------------------------------------------------------

def _rgba_to_hex(color: dict) -> str:
    r = int(color.get("r", 0) * 255)
    g = int(color.get("g", 0) * 255)
    b = int(color.get("b", 0) * 255)
    return f"#{r:02X}{g:02X}{b:02X}"


def extract_ui_specs(node: dict) -> dict:
    """Extract UI element specifications (dimensions, colors, typography, layout) from a node."""
    specs: dict = {
        "type": node.get("type"),
        "name": node.get("name"),
        "dimensions": {},
        "position": {},
        "colors": {},
        "typography": {},
        "effects": [],
        "constraints": {},
        "layout": {},
    }
    if "absoluteBoundingBox" in node:
        bbox = node["absoluteBoundingBox"]
        specs["dimensions"] = {"width": bbox.get("width"), "height": bbox.get("height")}
        specs["position"] = {"x": bbox.get("x"), "y": bbox.get("y")}
    if "fills" in node and node["fills"]:
        fills = []
        for fill in node["fills"]:
            if fill.get("type") == "SOLID" and "color" in fill:
                c = fill["color"]
                fills.append({"r": c.get("r", 0), "g": c.get("g", 0), "b": c.get("b", 0), "a": c.get("a", 1), "hex": _rgba_to_hex(c)})
        specs["colors"]["fills"] = fills
    if "strokes" in node and node["strokes"]:
        strokes = []
        for stroke in node["strokes"]:
            if stroke.get("type") == "SOLID" and "color" in stroke:
                c = stroke["color"]
                strokes.append({"hex": _rgba_to_hex(c), "weight": node.get("strokeWeight", 1)})
        specs["colors"]["strokes"] = strokes
    if "style" in node:
        s = node["style"]
        specs["typography"] = {
            "fontFamily": s.get("fontFamily"),
            "fontSize": s.get("fontSize"),
            "fontWeight": s.get("fontWeight"),
            "lineHeight": s.get("lineHeightPx"),
            "letterSpacing": s.get("letterSpacing"),
            "textAlign": s.get("textAlignHorizontal"),
        }
    if "effects" in node:
        for eff in node["effects"]:
            if eff.get("visible", True):
                specs["effects"].append({"type": eff.get("type"), "radius": eff.get("radius"), "offset": eff.get("offset"), "color": eff.get("color")})
    if "constraints" in node:
        c = node["constraints"]
        specs["constraints"] = {"horizontal": c.get("horizontal"), "vertical": c.get("vertical")}
    if node.get("layoutMode"):
        specs["layout"] = {
            "mode": node.get("layoutMode"),
            "primaryAxisSizingMode": node.get("primaryAxisSizingMode"),
            "counterAxisSizingMode": node.get("counterAxisSizingMode"),
            "paddingLeft": node.get("paddingLeft"),
            "paddingRight": node.get("paddingRight"),
            "paddingTop": node.get("paddingTop"),
            "paddingBottom": node.get("paddingBottom"),
            "itemSpacing": node.get("itemSpacing"),
        }
    return specs


def traverse_and_extract(node: dict, depth: int = 0, max_depth: int = 10) -> list[dict]:
    """Recursively extract UI specs from a node tree."""
    if depth > max_depth:
        return []
    specs = [extract_ui_specs(node)]
    specs[-1]["depth"] = depth
    for child in node.get("children", []):
        specs.extend(traverse_and_extract(child, depth + 1, max_depth))
    return specs


def extract_design_tokens(file_data: dict) -> dict:
    """Extract design tokens (colors, typography, effects) from file styles."""
    tokens: dict = {"colors": {}, "typography": {}, "effects": {}}
    for style_id, style in (file_data.get("styles") or {}).items():
        stype = style.get("styleType")
        name = style.get("name", style_id)
        if stype == "FILL":
            tokens["colors"][name] = style
        elif stype == "TEXT":
            tokens["typography"][name] = style
        elif stype == "EFFECT":
            tokens["effects"][name] = style
    return tokens
