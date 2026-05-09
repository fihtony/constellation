# Web Agent — Definition of Done

A task is complete when ALL of the following are true:

## Code Quality

- [ ] All specified features are implemented and match the task description.
- [ ] No linting errors in modified files (run `run_validation_command(validation_type="lint")` if lint is configured).
- [ ] Code follows the existing style conventions of the repository.
- [ ] No commented-out code or debug statements left behind.

## Build and Tests

- [ ] Build passes (`run_validation_command(validation_type="build")`).
- [ ] Unit tests pass (`run_validation_command(validation_type="unit_test")`).
- [ ] No regressions introduced in existing tests.

## SCM / PR

- [ ] Changes committed on a new branch (not main/master/develop).
- [ ] Branch name follows the convention: `feature/<jira-key>-<description>` or `chore/<description>`.
- [ ] PR created with a descriptive title and body.
- [ ] PR body references the Jira ticket if one was provided.

## Evidence

- [ ] `collect_task_evidence` completed — logs, diffs, and artifact paths captured.
- [ ] PR URL included in the artifact metadata.
- [ ] Branch name included in the artifact metadata.
- [ ] Validation results (passed/failed) included in the summary.

## Jira

- [ ] If a Jira ticket was provided and `jiraInReview` was requested: ticket status updated via `jira_add_comment` or transition.
