# Compass Agent — Definition of Done

A task routed through Compass is complete when ALL of the following are met:

1. **Downstream agent callback received** — Team Lead or Office Agent has sent a callback with `TASK_STATE_COMPLETED`.
2. **Required evidence present** — For development tasks: `prUrl` and `branch` are in the callback artifact metadata.
3. **Jira status updated** — For development tasks with a ticket: `jiraInReview` is `true` in the callback metadata.
4. **User-facing summary produced** — A clear, non-technical summary has been presented to the user.
5. **No pending clarification** — No open `INPUT_REQUIRED` state remains.
