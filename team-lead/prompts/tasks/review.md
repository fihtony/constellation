# Team Lead Agent — Code Review

When reviewing execution-agent output, check these criteria in order:

## Evidence Checks (Mandatory)

1. **PR URL present** — `metadata.prUrl` must be in the callback artifacts.
2. **Branch name valid** — must not be `main`, `master`, `develop`, or `release/*`.
3. **Jira updated** — `metadata.jiraInReview` must be `true`.
4. **No critical test failures** — check `test-results.json` summary in callback.

## Code Quality Checks

5. **Implementation completeness** — does the PR description match the Jira acceptance criteria?
6. **No hardcoded credentials or sensitive data** — scan PR description and any diff summary.
7. **Appropriate test coverage** — at least unit tests for new logic.

## Review Decision

- **Accept**: all mandatory checks pass and code quality is satisfactory → mark COMPLETED.
- **Request revision**: one or more checks fail but the issue is fixable → dispatch revision.
- **Reject**: max revisions exceeded, or a fundamental design error → mark FAILED.

## Revision Message Format

When requesting a revision, include:
- What passed (to avoid regressing it).
- What failed (specific, actionable).
- What the agent must change or add.
