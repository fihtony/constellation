# Team Lead Output Contract

## Required Task Outputs

- A staged execution plan with scope, risks, and ownership.
- Stage-level summaries for planning, implementation review, testing review, and wrap-up.
- Review notes that explain why a checkpoint passed or failed.
- Final summary with acceptance status, artifact references, and residual risks.

## Progress Events

- Emit stage start, stage approval, rework requested, waiting for input, completed, and failed.
- Every progress event should include `agentId`, `taskId`, `stage`, `summary`, and `timestamp`.

## Evidence Requirements

- Reference execution evidence instead of paraphrasing it away.
- Keep links or metadata for commands, tests, review comments, and generated artifacts.

## Failure Output

- Explain the failing stage.
- Explain whether the failure is retryable, blocked on user input, or terminal.
