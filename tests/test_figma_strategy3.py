#!/usr/bin/env python3
"""Test strategy 3: meta + targeted node fetch (2 API calls)."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "ui-design"))
from common.env_utils import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests", ".env"))
os.environ["FIGMA_MIN_CALL_INTERVAL_SECONDS"] = "0"
import figma_client

figma_url = os.environ.get("TEST_FIGMA_FILE_URL", "")
if not figma_url:
    raise RuntimeError("TEST_FIGMA_FILE_URL must be set in tests/.env")

file_key, node_id = figma_client.parse_figma_url(figma_url)
if not file_key or not node_id:
    raise RuntimeError("TEST_FIGMA_FILE_URL must include a valid Figma file URL and node/focus id")

t0 = time.time()
meta, s1 = figma_client.fetch_file_meta(file_key)
t1 = time.time()
print(f"meta: {t1-t0:.1f}s, name={meta.get('name')}")

time.sleep(1)
t0 = time.time()
node, s2 = figma_client.fetch_nodes(file_key, [node_id])
t1 = time.time()
raw = json.dumps(node)
print(f"node: {t1-t0:.1f}s, size={len(raw)/1024:.0f}KB")

nodes_data = node.get("nodes", {})
for nid, ndata in nodes_data.items():
    doc = ndata.get("document", {})
    def count_nodes(n):
        c = 1
        for ch in n.get("children", []):
            c += count_nodes(ch)
        return c
    total = count_nodes(doc)
    print(f"  node {nid}: type={doc.get('type')}, name={doc.get('name')}, children={total}")

print(f"\nStrategy 3: 2 API calls, gets metadata + target node tree")
print("This is the OPTIMAL approach for the Constellation workflow.")
