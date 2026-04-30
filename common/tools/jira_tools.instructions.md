# Jira Tools — Usage Guide

## Available Tools

### `jira_get_ticket`
Fetch complete Jira ticket details (summary, description, status, assignee, labels, attachments).

**When to use**: At the start of a task to understand requirements, or when references like "PROJ-123" appear in the task description.

**Best practice**: Fetch the ticket early. Store the summary and acceptance criteria for reference throughout the implementation.

### `jira_add_comment`
Add a progress comment to a Jira ticket.

**When to use**: After completing a significant milestone (e.g., "PR created at https://...").

**Constraints**:
- Do not add comments for every minor step — only major milestones.
- Keep comments concise and factual.
- Include PR links when available.

## Error Handling
If the Jira Agent is unavailable, these tools return an error. Continue the task without Jira context rather than blocking — log the error and proceed.
