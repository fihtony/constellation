# Compass Agent — Completeness Validation Policy

## What to Validate on Task Completion

After receiving a Team Lead callback, verify that the artifact metadata contains:

1. `prUrl` — a pull request was created.
2. `branch` — the working branch name.
3. `jiraInReview` — Jira ticket was transitioned to "In Review" (boolean true).

## If Validation Fails

1. Trigger one follow-up cycle: send a clarification message to Team Lead describing what is missing.
2. If the follow-up also fails, produce a partial-success summary to the user explaining what succeeded and what is still pending.

## What NOT to Validate

- Do not scan execution-agent workspace subdirectories (`android-agent/`, `web-agent/`).
- Do not call Jira or SCM directly to verify the PR or ticket status.
