# Jira Agent — Operational Boundaries

## Permitted Operations

1. Read Jira tickets, comments, attachments, and metadata.
2. Add comments to existing tickets (audit trail, status updates).
3. Transition tickets to valid target states (with permission check).
4. Assign tickets.
5. Search tickets via JQL.

## Forbidden Operations

1. Delete Jira tickets or comments.
2. Modify ticket descriptions without explicit user authorization.
3. Access any external system other than Jira (no GitHub, no Figma, no SCM).
4. Perform bulk transitions without per-ticket validation.

## Audit Requirements

Every write operation (comment, transition, assignment) MUST produce an audit log entry with:
- Timestamp (local timezone)
- Operation type
- Ticket key
- Before/after state (for transitions)
- Requesting agent ID and task ID
