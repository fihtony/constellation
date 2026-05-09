# Web Agent — Failure Handling

## Failure Categories

| Category | Examples | Action |
|----------|---------|--------|
| **Workspace Error** | Clone failed, workspace inaccessible, disk full | `fail_current_task` immediately |
| **Build Failure** | Compile error, missing dependency | Fix once, retry. If second failure: `summarize_failure_context` + `fail_current_task` |
| **Test Failure** | Unit test assertion error | Fix once, retry. Same rule as build failure |
| **SCM Error** | Branch already exists, push denied, PR creation failed | Retry with a unique branch name; if still failing: `fail_current_task` |
| **Ambiguous Task** | Tech stack unclear, conflicting requirements | Ask Team Lead via `request_agent_clarification` first |

## Structured Failure Output

When calling `fail_current_task`, always include:

```json
{
  "reason": "One sentence description of the failure",
  "failureContext": {
    "failureDescription": "...",
    "errorOutput": "... (last 500 chars of error log)",
    "affectedComponents": ["path/to/file.ts", "package.json"],
    "suggestedNextSteps": [
      "Verify that the repository has a valid package.json",
      "Check if the test runner is configured correctly"
    ],
    "retriable": true
  }
}
```

## Recovery Budget

- 1 recovery cycle per validation type (build OR unit_test).
- 1 retry for SCM errors with a different branch name suffix.
- 0 retries for workspace errors or permission denials — escalate immediately.
- Never spend more than 3 total recovery attempts before failing.

## Non-Retriable Errors

These errors should trigger immediate `fail_current_task` without retry:

- `PERMISSION_DENIED` on any SCM or boundary agent operation
- Workspace path outside the allowed `sharedWorkspacePath`
- Protected branch write attempt
- Task description is fundamentally incomplete (missing repo URL when required, no tech stack clue)
