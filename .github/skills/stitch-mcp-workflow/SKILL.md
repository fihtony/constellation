---
name: stitch-mcp-workflow
description: 'Google Stitch MCP workflow for authentication, tools/list discovery, project metadata fetch, screen listing (list_screens / find_screen_by_name), screen design/code retrieval, screen image fetch, and agent endpoint validation via the ui-design-agent. Use when implementing or testing Google Stitch design-context flows.'
user-invocable: true
---

# Google Stitch MCP Workflow

## When To Use

- Validate a Google Stitch API key by calling `tools/list`.
- Fetch the available MCP tool set from the Stitch MCP server.
- Retrieve metadata for a Google Stitch design project.
- **List all screens in a project** using `list_screens` / `find_screen_by_name`.
- Fetch design data and generated code for a specific screen.
- Retrieve the image preview for a screen.
- Test the `ui-design-agent` Stitch endpoints: `/stitch/tools`, `/stitch/project`, `/stitch/screen`, `/stitch/screen/image`.
- Invoke A2A skills `stitch.project.get` or `stitch.screen.fetch` from a development agent.

## Architecture

Stitch access is handled exclusively by the `ui-design-agent` (port 8040).  
Other agents **must not** call the Stitch MCP server directly — they call `ui-design-agent` via A2A.  
Client code lives at `ui-design/stitch_client.py` (local to the agent, not in `common/`).

```
Development Agent
  └─► POST /message:send  {requestedCapability: "stitch.screen.fetch", ...}
         └─► ui-design-agent
                └─► POST https://stitch.googleapis.com/mcp   (JSON-RPC 2.0)
                       method: tools/call
                       params: {name: "get_screen", arguments: {...}}
```

## Authentication

- Use the `X-Goog-Api-Key` request header with a **Google / Gemini API key**.
- Generate a key at: https://aistudio.google.com/app/apikey.
- Set via environment variable `STITCH_API_KEY` in `ui-design/.env`.
- The `stitch_client._stitch_post()` function injects the key automatically.

## MCP Protocol

Google Stitch uses **JSON-RPC 2.0** over HTTPS POST:

```
POST https://stitch.googleapis.com/mcp
Content-Type: application/json
X-Goog-Api-Key: <api_key>

{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "<method>",
  "params": { ... }
}
```

## Available MCP Methods

| Method | Purpose |
|--------|---------|
| `tools/list` | Discover available Stitch tools |
| `tools/call` with `name: "get_project"` | Fetch project metadata |
| `tools/call` with `name: "list_screens"` | List all screens in a project |
| `tools/call` with `name: "get_screen"` | Fetch screen design + generated code |
| `tools/call` with `name: "get_screen_image"` | Fetch screen image (may not be available in all versions) |

### `get_project` params
```json
{"name": "get_project", "arguments": {"name": "projects/{projectId}"}}
```

### `list_screens` params
```json
{"name": "list_screens", "arguments": {"project_id": "{projectId}"}}
```

Returns a list of screen objects, each with at least `id` (32-char hex) and `name` (string) fields.

### `get_screen` params
```json
{"name": "get_screen", "arguments": {"project_id": "{projectId}", "screen_id": "{screenId}"}}
```

## Client Helper Functions (`ui-design/stitch_client.py`)

| Function | Signature | Description |
|----------|-----------|-------------|
| `list_tools()` | `() → (list, status)` | Lists all available Stitch MCP tools |
| `get_project(project_id)` | `(str) → (dict, status)` | Fetches project metadata |
| `list_screens(project_id)` | `(str) → (list[dict], status)` | Lists all screens; each item has `id` + `name` |
| `find_screen_by_name(project_id, page_name)` | `(str, str) → (dict\|None, status)` | Finds a screen by exact or partial name match |
| `get_screen(project_id, screen_id)` | `(str, str) → (dict, status)` | Fetches screen design + code |
| `get_screen_image(project_id, screen_id)` | `(str, str) → (dict, status)` | Fetches screen image URL |

### `list_screens` usage
```python
from ui_design.stitch_client import list_screens, find_screen_by_name

# List all screens
screens, status = list_screens(project_id)
# screens: [{"id": "4cb76ffb...", "name": "Lesson Library"}, ...]

# Find a screen by name (case-insensitive, partial match)
screen, status = find_screen_by_name(project_id, "Lesson Library")
if screen:
    screen_id = screen["id"]   # → "4cb76ffb69624ddeb01b16075909d929"
```

`find_screen_by_name` tries (in order):
1. Exact case-insensitive match
2. Search term is a substring of screen name
3. Screen name is a substring of search term

## ID Formats

| Identifier | Format | Example |
|------------|--------|---------|
| `projectId` | 18–20 digit integer (string) | `13629074018280446337` |
| `screenId` | 32-character hex string | `4cb76ffb69624ddeb01b16075909d929` |

## Agent HTTP Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /stitch/tools` | List available Stitch MCP tools |
| `GET /stitch/project?id={projectId}` | Project metadata |
| `GET /stitch/screen?project_id={projectId}&screen_id={screenId}` | Screen design + code |
| `GET /stitch/screen/image?project_id={projectId}&screen_id={screenId}` | Screen image |

## A2A Skills

| Skill ID | Description |
|----------|-------------|
| `stitch.project.get` | Project metadata via MCP |
| `stitch.screen.fetch` | Screen design and generated code via MCP (auto-resolves page name → screen ID via `list_screens`) |
| `stitch.screen.image` | Screen image preview via MCP |

## Error Handling

- `isError: true` in the MCP result body indicates an application-level error (e.g., resource not found).
- HTTP 401/403 means the API key is invalid or missing.
- `get_screen_image` returns `status: "tool_not_found"` when the tool is not yet available in the current Stitch MCP version — treat this gracefully.
- `list_screens` returns `([], "error_401")` or `([], "error_403")` when OAuth2 is required; handle as SKIP in tests.

## Test Targets

```python
STITCH_PROJECT_ID = "13629074018280446337"               # Open English Study Hub
STITCH_SCREEN_ID  = "4cb76ffb69624ddeb01b16075909d929"  # Lesson Library
```

Run the test suite:
```bash
# Dry-run (no network):
python3 tests/test_stitch_mcp.py

# Full integration (requires STITCH_API_KEY in tests/.env):
python3 tests/test_stitch_mcp.py --integration

# Or via ui-design-agent integration tests:
python3 tests/test_ui_design_agent.py --integration --stitch
python3 tests/test_ui_design_agent.py --integration --agent-url http://127.0.0.1:8041
```

## Test Cases

| Test | Description |
|------|-------------|
| `test_stitch_mcp_tools_list` | `tools/list` returns ≥ 1 tool |
| `test_stitch_mcp_get_project` | `get_project` returns project metadata |
| `test_stitch_mcp_get_screen` | `get_screen` returns screen design/code |
| `test_stitch_mcp_get_screen_image` | `get_screen_image` returns image URL or skips gracefully |
| `test_stitch_mcp_list_screens` | `list_screens` returns a list of screen objects with `id`+`name` |
| `test_stitch_mcp_find_screen_by_name` | `find_screen_by_name` locates the first screen by partial name match |
