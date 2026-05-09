# Jira Agent — Validation Policy

## Validating Jira Responses

1. After fetching a ticket, verify the response contains required fields: `key`, `summary`, `status`.
2. After adding a comment, verify the comment ID is returned (confirms write succeeded).
3. After a transition, re-fetch the ticket status to confirm the transition took effect.

## Data Sanitization

1. Never expose raw Jira API credentials in logs or artifacts.
2. Truncate ticket descriptions to 30720 bytes when returning context to upstream agents.
3. Mark the `truncated: true` flag if content was truncated.
