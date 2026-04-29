# Web Agent Output Contract

## Required Task Outputs

- A concise implementation summary.
- A list of changed files or generated artifacts.
- Validation evidence: commands, outcomes, and notable failures.
- Residual risks, deferred work, or required follow-up actions.

## Artifact Expectations

- Store stage summaries, command logs, and test results in the shared workspace.
- Include metadata that ties artifacts back to `taskId`, `agentId`, and workflow stage.

## Review Feedback Handling

- When Team Lead requests rework, respond with a delta summary that focuses on the addressed feedback.
- Preserve prior evidence instead of overwriting it with a new unrelated report.

## Failure Output

- Return the failing command or operation.
- State whether the failure was auto-repairable, escalated, or terminal.
