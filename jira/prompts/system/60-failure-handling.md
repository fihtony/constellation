# Jira Agent — Failure Handling

## Authentication Failures

- Report immediately with `fail_current_task`.
- Include: auth mode used, whether token was present, Jira base URL.
- Never log the actual token value.

## Ticket Not Found

- Report with `fail_current_task` and include the ticket key.
- Do NOT attempt to create a substitute ticket.

## Transition Failures

- Report current status and list of valid transitions.
- Suggest the correct transition to the upstream agent.

## Rate Limiting

- Retry up to 3 times with exponential backoff (2s, 4s, 8s).
- If still failing after 3 retries, fail with `rate_limit_exceeded` reason.
