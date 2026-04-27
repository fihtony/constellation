# Compass Output Contract

## Required Task Outputs

- A user-facing task summary for every terminal state.
- Structured task status with `taskId`, `state`, and `updatedAt`.
- Child task references for routed work.
- Input request payloads when the task cannot continue without the user.

## Progress Events

- Emit major progress only: task accepted, routed, waiting for input, completed, failed.
- Each progress event should include `agentId`, `step`, `summary`, and `timestamp`.

## Final Response

- Include a concise outcome summary.
- Include relevant artifact references returned by Team Lead.
- Include residual risks or required next user action when applicable.

## Failure Output

- Preserve the downstream failing agent and state.
- Return a user-safe summary instead of raw internal stack traces.
