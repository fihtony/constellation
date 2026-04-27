---
name: jira-cloud-workflow
description: 'Jira Cloud workflow for auth diagnosis, scoped-token gateway handling, current-user lookup, JQL search, ticket fetch/create/update validation, comment CRUD, safe reversible field updates, safe reversible transition testing, assignee restore, and message-flow testing against the configured Jira site. Supports both REST API (default) and Atlassian Rovo MCP backends.'
user-invocable: true
---

# Jira Cloud Workflow

## When To Use

- Diagnose Jira Cloud authentication for the configured Jira agent.
- Search Jira issues with JQL.
- Confirm which Jira user is authenticated.
- Fetch a ticket and inspect current status, assignee, comments, or attachments.
- Validate Jira issue-create payloads safely before any shared-environment write.
- Update editable issue fields such as summary, description, labels, or priority.
- Add, update, or delete a Jira comment.
- Test a real status transition and restore the original state safely.
- Change an assignee for validation and restore the original assignee.

## Runtime Packaging

- `jira/app.py` reads this `SKILL.md` at runtime and injects it into the LLM prompt inside `process_message()`.
- When the Jira agent runs in Docker, the image must contain `.github/skills/jira-cloud-workflow/SKILL.md`.
- The current `jira/Dockerfile` copies `.github/skills/` into `/app/.github/skills/` so prompt injection works in containers as well as local runs.

## Backend Selection

The Jira agent supports two back-end implementations, selectable via `JIRA_BACKEND`:

| Value | Description |
|-------|-------------|
| `rest` (default) | Direct Jira REST API v3 calls to your Atlassian site |
| `mcp` | Atlassian Rovo MCP server (`https://mcp.atlassian.com/v1/mcp`) |

Set in `jira/.env` or as a Docker environment variable:
```env
# [OPTIONAL] rest (default) | mcp
JIRA_BACKEND=rest
```

Both backends use the same `JIRA_TOKEN` / `JIRA_EMAIL` credentials and expose the same HTTP endpoints. The `GET /health` response reports the active backend:
```json
{"status": "ok", "agent_id": "jira-agent", "backend": "rest"}
```

### Provider classes

| Class | File | Backend |
|-------|------|---------|
| `JiraRESTProvider` | `jira/providers/rest.py` | REST API v3 |
| `JiraMCPProvider` | `jira/providers/mcp.py` | Atlassian Rovo MCP |

Both implement the abstract `JiraProvider` interface (`jira/providers/base.py`).

## Authentication

- For a raw Jira Cloud API token, use `Authorization: Basic base64(JIRA_EMAIL:JIRA_TOKEN)`.
- Do not assume raw `Bearer <JIRA_TOKEN>` works in this environment.
- Scoped Jira API tokens may require the Atlassian API gateway: `https://api.atlassian.com/ex/jira/{cloudId}/rest/api/3/...`.
- The Jira agent auto-discovers `cloudId` from `/_edge/tenant_info`; set `JIRA_CLOUD_ID` only when you need to skip discovery.
- If the configured token is already a full `Basic ...` or `Bearer ...` header value, the Jira agent will pass it through unchanged.

## Recommended Scopes

- Prefer classic scopes for a scoped token: `read:jira-user`, `read:jira-work`, `write:jira-work`.
- This scope set covers `/myself`, ticket fetch, JQL search, issue create/update, transitions, comment CRUD, and assignee writes used by the current Jira agent.
- Only add `read:app-user-token` or `read:app-system-token` when calling Jira from a Forge Remote backend.

## Atlassian Rovo MCP Provider (`JIRA_BACKEND=mcp`)

### Requirements

- Same `JIRA_TOKEN` (API token) and `JIRA_EMAIL` credentials as the REST backend.
- **MCP API token auth must be enabled** in your Atlassian Admin: `admin.atlassian.com → Security → MCP Server settings → Allow API token authentication`.
- When MCP API token auth is disabled, `JiraMCPProvider` automatically falls back to the REST backend for all operations.

### MCP Server

- URL: `https://mcp.atlassian.com/v1/mcp`
- Protocol: JSON-RPC 2.0 over MCP Streamable HTTP (SSE responses)
- All tool calls require a `cloudId` argument (auto-discovered from `/_edge/tenant_info`)

### MCP Tools Used

| MCP Tool | Operation |
|----------|-----------|
| `getJiraIssue` | `fetch_issue(key)` |
| `searchJiraIssuesUsingJql` | `search_issues(jql)` |
| `transitionJiraIssue` | `transition_issue(key, name)` |
| `createJiraIssue` | `create_issue(project, summary, type)` |
| `editJiraIssue` | `update_issue_fields(key, fields)`, `change_assignee(key, account_id)` |
| `addCommentToJiraIssue` | `add_comment(key, text)` |
| `lookupJiraAccountId` | internal — resolves email → account ID |

### REST Fallback Operations

These operations always use the REST backend even when `JIRA_BACKEND=mcp`:

| Method | Reason |
|--------|--------|
| `get_myself()` | No MCP tool available |
| `get_transitions(key)` | No MCP tool available |
| `update_comment(key, id, text)` | No MCP tool available |
| `delete_comment(key, id)` | No MCP tool available |

## Safe Validation Workflow

1. Resolve the ticket key and read the current status first.
2. List available transitions before attempting a state change.
3. Choose a non-terminal transition when possible.
4. Verify the new status after the transition.
5. List transitions again and restore the original status before ending the test.
6. Capture the original assignee before any assignee write.
7. Restore the original assignee after the test, using `null` only when the original assignee was empty.
8. Delete any temporary comments created for testing.
9. Prefer reversible field updates such as labels on an approved shared ticket, then restore the original labels.

## Useful Jira Agent Endpoints

- `GET /jira/myself`
- `GET /jira/search?jql=key=CSTL-1&maxResults=5&fields=summary,status`
- `GET /jira/tickets/{key}`
- `POST /jira/tickets` with `{"projectKey": "CSTL", "summary": "...", "issueType": "Task", "description": "..."}`
- `PUT /jira/tickets/{key}` with `{"fields": {"summary": "...", "labels": ["agent-test"]}}`
- `GET /jira/transitions/{key}`
- `POST /jira/transitions/{key}` with `{"transition": "In Progress"}`
- `POST /jira/comments/{key}` with `{"text": "..."}`
- `PUT /jira/comments/{key}/{id}` with `{"text": "..."}`
- `DELETE /jira/comments/{key}/{id}`
- `PUT /jira/assignee/{key}` with `{"accountId": "..."}` or `{"accountId": null}`

## Focused Test Commands

1. Jira REST agent regression (local): `python3 tests/test_jira_rest.py --integration -v`
2. Jira REST agent regression (container): `python3 tests/test_jira_rest.py --integration --container -v`
3. Jira MCP raw tool tests: `python3 tests/test_jira_mcp.py --integration -v`
4. Jira MCP provider class tests: `python3 tests/test_jira_mcp.py --integration --provider -v`
5. Shared test targets are centralized under `tests/agent_test_targets.py`; do not widen writes beyond that allowlist.

## Search And Update Notes

1. Jira Cloud has removed the legacy `/rest/api/3/search` endpoint for this tenant; use `/rest/api/3/search/jql`.
2. Keep JQL reads narrow during validation, such as `key = CSTL-1` or a bounded project query.
3. Prefer updating only explicit fields in `PUT /jira/tickets/{key}` and avoid broad field replacement.
4. In shared environments, validate `POST /jira/tickets` through input-validation paths unless the user explicitly approves creating a new issue.

## Guardrails

- Never leave a ticket in a different status after a validation run.
- Never leave a temporary comment behind.
- Never leave a different assignee behind.
- Never leave a temporary label behind.
- Prefer an explicitly approved test ticket for destructive validation.