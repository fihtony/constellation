# Jira Agent — Definition of Done

A Jira Agent task is complete when:

1. **Requested operation completed** — The Jira API confirmed the operation (fetch, comment, transition, assign).
2. **Audit log written** — All write operations have audit entries.
3. **Result artifact produced** — The task artifact contains the operation result in structured JSON.
4. **No permission violations** — All operations were performed within authorized scope.
