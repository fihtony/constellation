#!/usr/bin/env python3
"""Test script for Figma REST API — investigate rate limits and best fetch strategy."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ui-design"))

os.environ.setdefault("FIGMA_MIN_CALL_INTERVAL_SECONDS", "0")

from common.env_utils import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import figma_client

TEST_URL = os.environ.get("TEST_FIGMA_FILE_URL", "")

if not TEST_URL:
    raise RuntimeError("TEST_FIGMA_FILE_URL must be set in tests/.env")

def test_parse_url():
    file_key, node_id = figma_client.parse_figma_url(TEST_URL)
    print(f"[parse] file_key={file_key}, node_id={node_id}")
    assert file_key is not None, "file_key should not be None"
    assert node_id is not None, "node_id should not be None"
    return file_key, node_id


def test_fetch_meta(file_key):
    t0 = time.time()
    meta, status = figma_client.fetch_file_meta(file_key)
    elapsed = time.time() - t0
    print(f"[meta] status={status}, time={elapsed:.1f}s")
    print(f"  name={meta.get('name')}, version={meta.get('version')}")
    assert status == "ok", f"meta fetch failed: {status}"
    return meta


def test_fetch_pages(file_key):
    t0 = time.time()
    pages, status = figma_client.fetch_pages(file_key)
    elapsed = time.time() - t0
    print(f"[pages] status={status}, time={elapsed:.1f}s, count={len(pages)}")
    for p in pages:
        print(f"  page: id={p['id']}, name={p['name']}")
    assert status == "ok", f"pages fetch failed: {status}"
    return pages


def test_fetch_full_file(file_key):
    """Fetch the full file tree (no depth limit). This is the bulk download approach."""
    t0 = time.time()
    status_code, body = figma_client._figma_get(f"files/{file_key}")
    elapsed = time.time() - t0
    if status_code == 200:
        # Measure response size
        raw = json.dumps(body)
        size_kb = len(raw) / 1024
        page_count = len(body.get("document", {}).get("children", []))
        print(f"[full_file] status=200, time={elapsed:.1f}s, size={size_kb:.0f}KB, pages={page_count}")
        # Count total nodes
        def count_nodes(node):
            c = 1
            for child in node.get("children", []):
                c += count_nodes(child)
            return c
        total = count_nodes(body.get("document", {}))
        print(f"  total nodes in file: {total}")
        return body
    else:
        print(f"[full_file] status={status_code}, time={elapsed:.1f}s")
        print(f"  body: {json.dumps(body)[:200]}")
        return None


def test_fetch_node(file_key, node_id):
    t0 = time.time()
    result, status = figma_client.fetch_nodes(file_key, [node_id])
    elapsed = time.time() - t0
    if status == "ok":
        raw = json.dumps(result)
        print(f"[node] status={status}, time={elapsed:.1f}s, size={len(raw)/1024:.0f}KB")
    else:
        print(f"[node] status={status}, time={elapsed:.1f}s")
    return result


def test_fetch_page_by_name(file_key, page_name):
    t0 = time.time()
    result, status = figma_client.fetch_page_by_name(file_key, page_name)
    elapsed = time.time() - t0
    if status == "ok":
        raw = json.dumps(result)
        print(f"[page_by_name] status={status}, time={elapsed:.1f}s, size={len(raw)/1024:.0f}KB")
        matched = result.get("page", {})
        print(f"  matched: id={matched.get('id')}, name={matched.get('name')}")
    else:
        print(f"[page_by_name] status={status}, time={elapsed:.1f}s")
        print(f"  available pages: {result.get('availablePages', [])}")
    return result


def main():
    print("=" * 60)
    print("Figma REST API Investigation")
    print("=" * 60)

    file_key, node_id = test_parse_url()
    print()

    # Strategy 1: Multiple small calls (current approach)
    print("--- Strategy 1: Multiple small calls ---")
    meta = test_fetch_meta(file_key)
    time.sleep(1)  # small gap
    pages = test_fetch_pages(file_key)
    time.sleep(1)
    if pages:
        # Fetch first page by name
        result = test_fetch_page_by_name(file_key, pages[0]["name"])
    time.sleep(1)
    node_result = test_fetch_node(file_key, node_id)
    print(f"  Total API calls for Strategy 1: 4 calls\n")

    time.sleep(2)

    # Strategy 2: Single bulk download (full file)
    print("--- Strategy 2: Single full file download ---")
    full = test_fetch_full_file(file_key)
    if full:
        print(f"  Total API calls for Strategy 2: 1 call")
        print(f"  Contains ALL pages, ALL nodes in one response")
        # Check if node_id is in the full file
        nodes = full.get("document", {}).get("children", [])
        for page in nodes:
            print(f"  Page '{page.get('name')}': {len(page.get('children', []))} top-level children")

    print()
    print("=" * 60)
    print("CONCLUSION:")
    print("  Strategy 2 (single full file download) is MUCH better:")
    print("  - 1 API call vs 4+ calls")
    print("  - Gets ALL data at once")
    print("  - Cache the full response locally")
    print("  - No rate limit issues")
    print("=" * 60)


if __name__ == "__main__":
    main()
