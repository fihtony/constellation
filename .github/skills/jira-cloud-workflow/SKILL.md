---
name: jira-cloud-workflow
description: 'Jira Cloud workflow for auth diagnosis, scoped-token gateway handling, current-user lookup, JQL search, ticket fetch/create/update validation, comment CRUD, safe reversible field updates, safe reversible transition testing, assignee restore, and message-flow testing against the configured Jira site.'
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
- `GET /jira/search?jql=key=DMPP-2647&maxResults=5&fields=summary,status`
- `GET /jira/tickets/{key}`
- `POST /jira/tickets` with `{"projectKey": "DMPP", "summary": "...", "issueType": "Task", "description": "..."}`
- `PUT /jira/tickets/{key}` with `{"fields": {"summary": "...", "labels": ["agent-test"]}}`
- `GET /jira/transitions/{key}`
- `POST /jira/transitions/{key}` with `{"transition": "In Progress"}`
- `POST /jira/comments/{key}` with `{"text": "..."}`
- `PUT /jira/comments/{key}/{id}` with `{"text": "..."}`
- `DELETE /jira/comments/{key}/{id}`
- `PUT /jira/assignee/{key}` with `{"accountId": "..."}` or `{"accountId": null}`

## Focused Test Commands

1. Local Jira regression: `./venv/bin/python tests/test_jira_agent.py -v`
2. Container Jira regression: `./venv/bin/python tests/test_jira_agent.py --container -v`
3. Shared test targets are centralized under `tests/agent_test_targets.py`; do not widen writes beyond that allowlist.

## Search And Update Notes

1. Jira Cloud has removed the legacy `/rest/api/3/search` endpoint for this tenant; use `/rest/api/3/search/jql`.
2. Keep JQL reads narrow during validation, such as `key = DMPP-2647` or a bounded project query.
3. Prefer updating only explicit fields in `PUT /jira/tickets/{key}` and avoid broad field replacement.
4. In shared environments, validate `POST /jira/tickets` through input-validation paths unless the user explicitly approves creating a new issue.

## Guardrails

- Never leave a ticket in a different status after a validation run.
- Never leave a temporary comment behind.
- Never leave a different assignee behind.
- Never leave a temporary label behind.
- Prefer an explicitly approved test ticket such as `DMPP-2647` for destructive validation.