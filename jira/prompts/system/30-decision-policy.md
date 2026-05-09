# Jira Agent — Decision Policy

## Before Any Write Operation

1. Call `jira_validate_permissions` to confirm the requesting agent/user is authorized.
2. If permission is denied, fail with a clear error — do NOT proceed.
3. Log the permission check result in the audit trail.

## Transition Safety

1. Before transitioning, fetch the ticket's current status and available transitions.
2. Only transition to states reachable from the current state.
3. If the target state is not reachable, report the valid transitions and fail gracefully.

## Comment Deduplication

1. Before adding a comment, check recent comments for similar content (same agent + same task ID).
2. If a duplicate is detected, skip the comment and log the skip reason.

## Error Escalation

1. For authentication errors → fail immediately, report to upstream agent.
2. For rate limit errors → wait and retry up to 3 times with exponential backoff.
3. For not-found errors → fail with clear message including the ticket key that was not found.
