# Jira Agent — Available Tools

## Jira-Specific Tools

- `jira_issue_lookup` — Fetch full details of a Jira ticket by key.
- `jira_search` — Run a JQL query and return matching tickets.
- `jira_comment` — Add a comment to a Jira ticket.
- `jira_transition` — Transition a ticket to a new status.
- `jira_assign` — Assign a ticket to a user.
- `jira_validate_permissions` — Check if the requesting agent has permission for an operation.

## Common Tools

- `report_progress` — Report progress for long-running operations.
- `complete_current_task` — Mark the current task as complete with results.
- `fail_current_task` — Mark the current task as failed with a structured error.
- `load_skill` — Load a Jira workflow skill for guidance.
