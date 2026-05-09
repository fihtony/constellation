# Team Lead Agent — Failure Handling

## Error Classification

| Category | Examples | Action |
|----------|---------|--------|
| **User input error** | Missing Jira ticket, unknown platform | Pause → ask user (INPUT_REQUIRED) |
| **Transient system error** | Boundary agent timeout, registry unreachable | Retry once → continue with partial context |
| **Execution agent failure** | Build error, test failure, PR creation failed | Review and request revision (up to max_revisions) |
| **Permanent failure** | Max revisions exceeded, permission denied, no repo access | Mark FAILED → post Jira audit comment |

## Escalation Rules

1. **INPUT_REQUIRED**: Pause workflow. Emit a clear, specific question. Do not guess.
2. **Revision**: Dispatch the same agent again (same container, new task). Summarize what was wrong.
3. **FAILED**: Post a Jira audit comment explaining: what was attempted, what failed, what the user should do next.

## Jira Audit on Failure

When marking a task FAILED, always:
1. Post a comment to the Jira ticket (if a ticket key is known) explaining the rejection reason.
2. Include: attempted PR URL (if any), branch name (if any), test failure summary.
3. Set the ticket status to the appropriate state (e.g. "Rejected", "In Development").

## Resilience Rules

- Do not surface internal stack traces to the user — summarize in plain language.
- If a stage-summary.json write fails, log the error but continue the workflow.
- If progress reporting fails, log the error but continue — progress is best-effort.
