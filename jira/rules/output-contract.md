# Jira Agent Output Contract

## Required Outputs

- Explicit target reference: ticket key, URL, or query.
- Structured issue payload with status, summary, assignee, and relevant metadata.
- Structured mutation result when a write action occurs.
- Error category when auth, validation, or permission checks fail.

## Mutation Evidence

- Include which fields, comments, or transitions were changed.
- Preserve before/after summaries when the action mutates Jira state.

## Failure Output

- State whether the failure was caused by missing input, auth, permissions, or ticket-state conflicts.
