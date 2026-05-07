# Jira Agent — Available Tools

## Read Tools

- `jira_issue_lookup` — Fetch full details of a Jira ticket by key (summary, description, status, comments, attachments).
- `jira_search` — Run a JQL query and return matching tickets with key, summary, status, and assignee.
- `jira_get_myself` — Get the current authenticated Jira user's account ID and display name.
- `jira_get_transitions` — Get available workflow transitions for a Jira issue (call before transitioning).
- `jira_validate_permissions` — Check if the requesting agent has permission for an operation.

## Write Tools

- `jira_comment` — Add a comment to a Jira issue (plain text or Markdown).
- `jira_update_comment` — Update an existing comment on a Jira issue.
- `jira_delete_comment` — Delete a comment from a Jira issue.
- `jira_transition` — Transition a ticket to a new status. Always call `jira_get_transitions` first.
- `jira_assign` — Assign a ticket to a user by their Jira account ID.
- `jira_create_issue` — Create a new Jira issue in a given project.
- `jira_update_fields` — Update fields (labels, priority, custom fields) on an existing issue.

## Common Tools

- `report_progress` — Report progress for long-running operations.
- `complete_current_task` — Mark the current task as complete with results.
- `fail_current_task` — Mark the current task as failed with a structured error.
- `load_skill` — Load a Jira workflow skill for guidance.
