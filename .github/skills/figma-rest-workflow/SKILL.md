---
name: figma-rest-workflow
description: 'Figma REST API workflow for authentication, file metadata fetch, page listing, page-by-name lookup (fuzzy match), node/element spec retrieval, and agent endpoint validation via the ui-design-agent. Use when implementing or testing Figma design data access flows.'
user-invocable: true
---

# Figma REST API Workflow

## When To Use

- Validate a Figma personal access token against a known file.
- Fetch file-level metadata (name, last modified, thumbnail URL, version).
- List all pages (canvases) in a Figma file.
- Retrieve all nodes for a named page (fuzzy match supported).
- Fetch the design spec for a specific element by node ID (size, colour, typography, layout constraints).
- Test the `ui-design-agent` Figma endpoints: `/figma/meta`, `/figma/pages`, `/figma/page`, `/figma/node`.
- Invoke the A2A skill `figma.node.get` from a development agent to retrieve element specs.

## Architecture

Figma access is handled exclusively by the `ui-design-agent` (port 8040).  
Other agents **must not** call the Figma API directly — they call `ui-design-agent` via A2A instead.  
Client code lives at `ui-design/figma_client.py` (local to the agent, not in `common/`).

```
Development Agent
  └─► POST /message:send  {requestedCapability: "figma.node.get", ...}
         └─► ui-design-agent
                └─► GET https://api.figma.com/v1/files/{fileKey}/nodes?ids={nodeId}
```

## Authentication

- Use the `X-Figma-Token` header with a Figma **personal access token** (PAT).
- Generate a PAT at: https://www.figma.com/settings → Personal access tokens.
- Set via environment variable `FIGMA_TOKEN` in `ui-design/.env`.
- The `figma_client._figma_get()` function injects the token automatically.

## Figma URL Format

Figma design URLs follow this pattern:
```
https://www.figma.com/design/{fileKey}/{fileName}?node-id={nodeId}&focus-id={focusId}&view=focus&m=dev
```

- `fileKey`: alphanumeric, ~22 characters (e.g. `UnNnrZvWg8tqz1316j6oFY`).
- `node-id` in URLs uses **dashes** (e.g. `2345-19645`); the API uses **colons** (e.g. `2345:19645`).
- `focus-id` (optional): In Figma dev-mode URLs, this points to the **section/frame container**
  rather than a nested sub-component. When present, `parse_figma_url()` prefers it over `node-id`.
- `figma_client.parse_figma_url()` handles dash→colon conversion and focus-id preference automatically.

### URL Parameter Priority

| Parameter | Role | API Use |
|-----------|------|---------|
| `focus-id` | Section/frame container (preferred in dev mode) | Used as target node ID |
| `node-id` | Page navigation / nested element | Fallback when no focus-id |

Example: A URL with `node-id=2345-19645&focus-id=2345-19574` will resolve to node `2345:19574`
because the focus-id typically represents the meaningful design section.

## REST Endpoints

| Operation | Path | Key parameters |
|-----------|------|---------------|
| File metadata | `GET /files/{fileKey}?depth=1` | `X-Figma-Token` header |
| List pages | `GET /files/{fileKey}?depth=1` | Extract `document.children` |
| Fetch nodes | `GET /files/{fileKey}/nodes?ids={nodeId1},{nodeId2}` | comma-separated node IDs |

## Agent HTTP Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /figma/meta?url={figmaUrl}` | File name, version, last modified |
| `GET /figma/pages?url={figmaUrl}` | All pages in the file |
| `GET /figma/page?url={figmaUrl}&name={pageName}` | Nodes for a named page (fuzzy) |
| `GET /figma/node?url={figmaUrl}&node_id={nodeId}` | Element spec for a specific node |

## A2A Skills

| Skill ID | Description |
|----------|-------------|
| `figma.file.meta` | High-level file metadata |
| `figma.file.pages` | Page list |
| `figma.page.fetch` | All nodes for a named page |
| `figma.node.get` | Element/component design spec by node ID |

## SSL / TLS (Corporate Environments)

- The `figma_client._ssl_ctx()` function creates an SSL context that optionally loads a custom CA bundle.
- Set `CORP_CA_BUNDLE` or `SSL_CERT_FILE` in `.env` to the path of your corporate CA `.pem` file.
- The Docker image installs `ca-certificates` and runs `update-ca-certificates` at build time.

## Rate Limiting

Figma REST API enforces per-token rate limits using a leaky-bucket algorithm:

| Plan | Limit |
|------|-------|
| Starter / Free | 10 requests/min |
| Professional | 15 requests/min |
| Enterprise | 20 requests/min |

### Rate Limit Strategy (3-Layer Cache)

1. **Proactive throttle**: `FIGMA_MIN_CALL_INTERVAL_SECONDS` (default 8s) enforces a minimum
   gap between consecutive API calls. At 8s interval ≈ 7.5 calls/min, safely within all plans.
2. **429 retry**: On HTTP 429, honour the `Retry-After` header value (uncapped by default).
   Exponential backoff with base 8s, max 5 retries.
3. **File-system cache (Layer 1)**: `FigmaCache` stores API responses on disk (default TTL 3600s).
   Repeated requests for the same file/node/page are served from cache without API calls.
   Cache key format: `figma_{operation}_{fileKey}_{hash}.json`.
4. **Workspace cache (Layer 2)**: Fetched Figma data is saved to the shared workspace
   (`ui-design/figma-data-*.json`) so downstream agents (Team Lead, dev agents) can
   read it without triggering additional API calls or going through the UI Design agent.
5. **Cross-task reuse (Layer 3)**: Within a single agent session, the in-memory `FigmaCache`
   serves subsequent requests for the same design file with zero latency.

### Optimal Fetch Strategy (Recommended: 2 API Calls)

For a typical development workflow targeting a specific screen or section:
1. `GET /files/{key}?depth=1` — file metadata + page list (fast, ~2KB response)
2. `GET /files/{key}/nodes?ids={nodeId}&depth=4` — target node tree (targeted, moderate size)

**Anti-patterns to avoid:**
- Fetching the full file tree (`GET /files/{key}` without depth limit) — can be 70+ MB for large files
- Fetching multiple nodes separately when one batch call suffices
- Re-fetching data that's already in the workspace cache

### Caching Best Practices

- Design data rarely changes within a single task execution → set TTL ≥ 1 hour for task workflows
- Always check workspace cache before calling the UI Design agent
- For iterative workflows (revisions), the workspace cache from a prior task persists — no refetch needed
- The `cached_fetch_nodes()` and `cached_fetch_file_meta()` methods handle all 3 cache layers transparently

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `FIGMA_MIN_CALL_INTERVAL_SECONDS` | `8` | Minimum seconds between API calls |
| `FIGMA_MAX_RETRY_WAIT_SECONDS` | `0` (uncapped) | Max wait on 429 retry |
| `FIGMA_CACHE_DIR` | `/tmp/figma_cache` | File-system cache directory |
| `FIGMA_CACHE_TTL` | `3600` | Cache TTL in seconds |

## Test Targets

```python
FIGMA_FILE_KEY = "gxd2LNayM2hh3V3qTlcyPF"   # Website Wireframes UI Kit
FIGMA_NODE_ID  = "1:470"                       # Example element node
```

Run the test suite:
```bash
# Dry-run (no network):
python3 tests/test_ui_design_agent.py

# Full integration (requires running agent + FIGMA_TOKEN):
python3 tests/test_ui_design_agent.py --integration --figma

# Against the container:
python3 tests/test_ui_design_agent.py --integration --agent-url http://127.0.0.1:8041
```
