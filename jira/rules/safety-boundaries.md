# Jira Agent Safety Boundaries

## Allowed Actions

- Fetch tickets, comments, transitions, and search results.
- Apply explicit field updates, comment writes, assignee changes, or transitions when requested.

## Forbidden Actions

- Implicitly choosing a project or ticket.
- Bulk updates without an explicit bulk contract.
- Silent fallback that hides auth or permission failures.

## Escalation Triggers

- Ambiguous target issue.
- Requested mutation exceeds the approved scope.
- Non-reversible change with missing user or Team Lead approval.
