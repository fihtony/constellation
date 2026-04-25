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
https://www.figma.com/design/{fileKey}/{fileName}?node-id={nodeId}&...
```

- `fileKey`: alphanumeric, ~22 characters (e.g. `gxd2LNayM2hh3V3qTlcyPF`).
- `node-id` in URLs uses **dashes** (e.g. `1-470`); the API uses **colons** (e.g. `1:470`).
- `figma_client.parse_figma_url()` handles this conversion automatically.

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

- Figma's free-tier REST API enforces rate limits. HTTP 429 responses are transient.
- The agent returns `{"status": "error_429"}` when rate-limited — treat this as a retry signal, not a code bug.
- Do not retry in tight loops; apply exponential back-off.

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
