# SCM Agent — Decision Policy

## Branch Naming

When creating branches for development tasks:
- Format: `feature/{jira-key}-{orchestrator-task-id}` (e.g., `feature/PROJ-123-task-0001`)
- For docs/tests-only changes: `chore/{description}` (no ticket key required)
- Never use names that match protected branch patterns.

## PR Creation Policy

Before creating a PR:
1. Verify the source branch exists and is not protected.
2. Verify there are commits on the branch (non-empty diff from base).
3. Include the Jira ticket key in the PR title if available.
4. Include a structured description with: summary, changes, acceptance criteria, test results.

## Conflict Detection

If a push fails due to conflicts:
1. Report the conflict details to the upstream agent.
2. Do NOT attempt automatic merge or rebase.
3. Let the upstream agent decide how to handle the conflict.
