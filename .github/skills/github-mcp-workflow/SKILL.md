---
name: github-mcp-workflow
description: >
  GitHub MCP server workflow using the remote cloud server (https://api.githubcopilot.com/mcp/)
  over HTTP (Streamable HTTP transport). Covers GitHubMCPProvider: repo search/inspect,
  branch create, push_files, PR CRUD, PR comments, tool discovery. Use when implementing
  or testing the MCP back-end for the SCM agent (SCM_BACKEND=mcp).
user-invocable: true
---

# GitHub MCP Workflow

## When To Use

- Run SCM agent operations through the remote GitHub MCP server instead of the REST API.
- Test or debug the `GitHubMCPProvider` class.
- Verify the GitHub MCP server tool set available for your token.
- Implement new SCM capabilities that map naturally to MCP tools.

## Authentication

Same token as REST — set `SCM_TOKEN` in `scm/.env`.
The remote MCP server authenticates via `Authorization: Bearer <token>` HTTP header.

Constellation isolation rule:
- Do not use host `GH_TOKEN`, `GITHUB_TOKEN`, `gh auth`, or system keychain state as MCP credentials.
- Agent execution may use only the dedicated token from `scm/.env`.
- Tests may use only the dedicated token from `tests/.env`; any child process that receives a file-backed override must be started with `CONSTELLATION_TRUSTED_ENV=1` after inherited host GitHub credentials have been stripped.

Required fine-grained repository permissions (same as REST):
- **Contents** → Read and write
- **Pull requests** → Read and write
- **Issues** → Read and write
- **Metadata** → Read-only (automatic)

## Provider Selection

```env
SCM_PROVIDER=github
SCM_BACKEND=mcp      # activate remote MCP back-end
```

No local Docker or Node.js installation required.

## Remote MCP Server

The remote server URL is: **`https://api.githubcopilot.com/mcp/`**

This is the stable (non-insiders) production endpoint. Authentication via PAT:
```http
POST https://api.githubcopilot.com/mcp/
Authorization: Bearer <github_pat>
Content-Type: application/json
Accept: application/json, text/event-stream
```

The server uses **MCP Streamable HTTP transport** (JSON-RPC 2.0 over HTTP POST).
Each request is a separate HTTP POST. The server may return a session ID
(`Mcp-Session-Id` response header) for stateful routing; include it in subsequent requests.

### MCP Initialize Handshake

Before calling tools, send the initialize request and then the `notifications/initialized` notification:

```python
# 1. Initialize
resp = _post({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "my-client", "version": "1.0"}
}})
session_id = response_headers.get("Mcp-Session-Id")   # save for subsequent requests

# 2. Initialized notification (fire-and-forget)
_post({"jsonrpc":"2.0","method":"notifications/initialized","params":{}})
```

### Response Format

Responses may be `application/json` (direct JSON-RPC) or `text/event-stream` (SSE):
```
data: {"jsonrpc":"2.0","id":1,"result":{...}}
```
Parse SSE by splitting lines, finding `data:` prefix, and parsing the JSON value.

## Capability Map

| Constellation Skill | MCP Tool | Key Parameters |
|---|---|---|
| `scm.repo.search` | `search_repositories` | `query`, `perPage` |
| `scm.repo.inspect` | `search_repositories` | `query="repo:owner/repo"`, `perPage=1` |
| `scm.branch.list` | `list_branches` | `owner`, `repo`, `perPage` |
| `scm.branch.create` | `create_branch` | `owner`, `repo`, `branch`, `from_branch` |
| `scm.git.push` | `push_files` | `owner`, `repo`, `branch`, `files`, `message` |
| `scm.pr.create` | `create_pull_request` → then `pull_request_read(get)` | `owner`, `repo`, `title`, `body`, `head`, `base` |
| `scm.pr.get` | `pull_request_read` | `owner`, `repo`, `pullNumber`, `method="get"` |
| `scm.pr.list` | `list_pull_requests` | `owner`, `repo`, `state`, `perPage` |
| `scm.pr.comment` | `add_issue_comment` | `owner`, `repo`, `issue_number`, `body` |
| `scm.pr.comment.list` | `issue_read` | `owner`, `repo`, `issue_number`, `method="get_comments"` |

Remote-read supplement:
- The current GitHub MCP toolset does not cover every Constellation `SCMProvider` method.
- `GitHubMCPProvider` therefore keeps MCP as the primary backend for repo search, branch, push, and PR flows, but may reuse the GitHub REST-compatible provider logic for required read-only methods such as remote file/dir access, code search, ref comparison, default branch lookup, and branch rules.
- Treat this as a provider-contract compatibility layer, not as runtime backend fallback between agentic backends.

## Key Tool Signatures

### push_files
```json
{
  "owner": "myorg",
  "repo": "my-repo",
  "branch": "feature/my-branch",
  "files": [
    {"path": "src/app.py", "content": "print('hello')"},
    {"path": "README.md", "content": "# My project"}
  ],
  "message": "feat: add landing page"
}
```
The branch must already exist. `GitHubMCPProvider.push_files()` auto-creates it via
`create_branch` if it is not found in `list_branches`.

### create_branch
```json
{
  "owner": "myorg",
  "repo": "my-repo",
  "branch": "feature/my-branch",
  "from_branch": "main"
}
```
Returns a git ref object: `{"ref": "refs/heads/...", "url": "...", "object": {...}}` — NOT a branch object.

### create_pull_request — IMPORTANT: minimal response
```json
{
  "owner": "myorg",
  "repo": "my-repo",
  "title": "My PR",
  "body": "description",
  "head": "feature/my-branch",
  "base": "main"
}
```
The remote server returns ONLY: `{"id": "...", "url": "https://github.com/.../pull/5"}`.
Extract the PR number from `url`, then call `pull_request_read` to get the full PR.
`GitHubMCPProvider.create_pr()` does this automatically.

### pull_request_read (method=get)
```json
{
  "owner": "myorg",
  "repo": "my-repo",
  "pullNumber": 42,
  "method": "get"
}
```
Returns JSON string in `result.content[].text`. Parse with `json.loads()`.

### issue_read (method=get_comments)
```json
{
  "owner": "myorg",
  "repo": "my-repo",
  "issue_number": 42,
  "method": "get_comments"
}
```
Works for both issues and PRs (PR numbers are issue numbers on GitHub).

## Session Management

`GitHubMCPProvider` keeps a **single persistent HTTP session** per provider instance:
- Lazy initialization on first `_call()` invocation.
- A `threading.Lock` serialises concurrent requests.
- Auto-reconnect (re-initialize) on HTTP error.
- Call `provider.close()` to delete the remote session cleanly.

## Response Parsing

MCP tools return `result.content[].text` as a JSON string.
Always parse with `json.loads()` before accessing fields:

```python
from scm.providers.github_mcp import _extract_text, _parse_json

resp = provider._call("list_branches", {"owner": "...", "repo": "...", "perPage": 100})
data = _parse_json(_extract_text(resp))   # → list[dict] of branch objects
```

Check for errors before parsing:
```python
from scm.providers.github_mcp import _is_error
if _is_error(resp):
    print("Error:", _extract_text(resp))
```

## Running Tests

```bash
# GitHubMCPProvider full test (creates branch + files + PR + comment)
python3 tests/test_github_mcp.py --integration --provider -v

# Raw remote MCP server verification (tools/list + basic tool calls)
python3 tests/test_github_mcp.py --integration --raw -v

# Both
python3 tests/test_github_mcp.py --integration -v
```

All tests read config exclusively from `tests/.env`:
- `TEST_GITHUB_TOKEN` — GitHub PAT
- `TEST_GITHUB_REPO_URL` — target repo URL

Treat `tests/.env` as the only valid source of GitHub credentials during MCP testing. Shell-exported GitHub tokens and host keychain state are out of policy.

## SCM Agent with MCP back-end

```bash
# In scm/.env:
SCM_BACKEND=mcp

# Or inline:
SCM_BACKEND=mcp python3 scm/app.py
```

## Common Errors

| Error | Cause | Fix |
|---|---|---|
| `MCP init failed: ...` | Auth error on initialize | Check `TEST_GITHUB_TOKEN` is valid |
| `error_parse` from `get_pr` | MCP returned non-JSON text | Check `_extract_text(resp)` for error |
| `push_failed: ...` | Push rejected | Check Contents: Read and write permission |
| `create_failed: Resource not accessible` | Token lacks Contents permission | Add Contents: Read and write |
| HTTP 401 | Invalid or expired token | Regenerate GitHub PAT |
| HTTP 403 | Token lacks required permission | Check fine-grained PAT permissions above |


# GitHub MCP Workflow

## When To Use
