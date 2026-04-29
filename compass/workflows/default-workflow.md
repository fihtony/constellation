# Compass Default Workflow

## Purpose

This workflow defines how Compass accepts a task, delegates execution, and keeps the user informed without doing implementation work itself.

## Stages

1. Intake: validate the request, create the top-level task, and preserve user context.
2. Route: resolve the right Team Lead capability and dispatch the task.
3. Track: receive progress updates, callbacks, or polling results.
4. Clarify: when the user asks follow-up questions, request a richer summary from Team Lead.
5. Resume: forward user answers back to the waiting downstream task.
6. Finalize: present the final summary, artifact references, and next steps.

## Checkpoints

- Never dispatch development work directly to execution agents.
- Never complete a task without a downstream terminal state or a clear local failure reason.
- Preserve the original task context when resuming an interrupted workflow.

## Failure Handling

- If routing fails, return a structured failure immediately.
- If downstream state is ambiguous, mark the task as blocked and request clarification instead of guessing.
