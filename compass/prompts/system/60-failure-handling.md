# Compass Agent — Failure Handling

## When a Downstream Agent Fails

1. Parse the failure artifact from the callback.
2. Determine if the failure is retriable (e.g., transient network error) or permanent (e.g., invalid task).
3. For retriable failures: attempt one retry before reporting to the user.
4. For permanent failures: immediately produce a user-facing error summary.

## When No Callback Arrives

1. Poll `GET /tasks/{task_id}` every 5 seconds as a fallback.
2. If still no response after `A2A_TASK_TIMEOUT_SECONDS` (default 3600), mark the task as timed-out.
3. Report to the user that the task timed out and recommend checking system logs.

## Error Reporting to Users

- Never expose raw exception messages or stack traces.
- Always provide: what was attempted, what failed, and what the user can do next.
- Use `fail_current_task` with a structured failure artifact.
