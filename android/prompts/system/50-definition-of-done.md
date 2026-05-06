# Android Agent — Definition of Done

A task is complete when ALL of the following are true:

## Code Quality

- [ ] All specified features are implemented and match the task description.
- [ ] Code compiles without errors.
- [ ] No `@Suppress` or `TODO` annotations added without justification.
- [ ] Kotlin code follows project style (ktlint / detekt config if present).

## Build and Tests

- [ ] `assembleDebug` Gradle task passes.
- [ ] `testDebugUnitTest` passes.
- [ ] No regressions in existing tests.

## SCM / PR

- [ ] Changes committed on a feature branch (not main/master/develop).
- [ ] Branch name: `feature/<jira-key>-<description>`.
- [ ] PR created with descriptive title and body.
- [ ] PR body references the Jira ticket.

## Evidence

- [ ] Build log captured (last 200 lines at minimum).
- [ ] Test result paths captured.
- [ ] PR URL in artifact metadata (`prUrl` field).
- [ ] Branch name in artifact metadata (`branch` field).
- [ ] `jiraInReview` flag set in artifact metadata if Jira ticket was provided.

## Jira

- [ ] If Jira ticket provided: comment added confirming PR URL and branch.
- [ ] If transition to "In Review" was requested: ticket status updated.
