# Team Lead Agent — Definition of Done

A task is considered **complete** when ALL of the following criteria are met:

## For Development Tasks (Android / iOS / Web)

1. ✅ A pull request (PR) has been created in the target repository.
2. ✅ The PR URL is present in the execution agent's callback artifacts (`metadata.prUrl`).
3. ✅ The branch name matches the expected pattern (ticket key + orchestrator task ID).
4. ✅ The Jira ticket status has been updated to "In Review" (`metadata.jiraInReview == true`).
5. ✅ The PR description includes: Jira ticket reference, implementation summary, test results.
6. ✅ No critical build or test failures in the execution agent's test results.

## For Information / Analysis Tasks

1. ✅ A written summary with references to sources (Jira ticket, design context, repo) has been produced.
2. ✅ Unresolved ambiguities are explicitly listed (not silently skipped).

## Evidence Requirements

- All evidence must arrive in Team Lead's A2A callback artifacts, NOT read directly from execution-agent workspace files.
- Execution agents must include `prUrl`, `branch`, and `jiraInReview` in their artifact metadata.

## When a Task is NOT Done

- A PR exists but the branch was created on `main` or `master` (protected branch violation).
- The execution agent returned `TASK_STATE_FAILED`.
- The Jira ticket was NOT updated.
- The PR description is missing required references.
