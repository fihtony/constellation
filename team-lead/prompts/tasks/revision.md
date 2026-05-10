# Team Lead Agent — Revision Dispatch

When sending a revision request to an execution agent:

1. **Reuse the same agent container** — do NOT launch a new container.
2. **Provide a bounded revision context**:
   - Previous task ID
   - What was accepted (to preserve)
   - What was rejected (specific, actionable rejection reason)
   - Updated acceptance criteria if they changed
3. **Set the revision counter** — track how many revisions have been attempted.
4. **Include the original context** — pass the same `jiraContext`, `designContext`, `repoWorkspacePath`, and `repoUrl` as the original dispatch.

## Revision Context Format

```json
{
  "revision": {
    "attempt": 2,
    "previous_task_id": "task-xxx",
    "accepted": ["PR created", "branch name correct"],
    "rejected": ["Missing unit tests for PaymentValidator", "PR description lacks Jira reference"],
    "instructions": "Add unit tests for PaymentValidator. Update PR description to include PROJ-123."
  }
}
```

## Max Revisions

When the revision limit is reached:
1. Do NOT dispatch another revision.
2. Post a Jira audit comment explaining all failed checks across all attempts.
3. Mark the task TASK_STATE_FAILED.
4. Send ACK to the execution agent.
