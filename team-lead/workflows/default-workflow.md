# Team Lead Default Workflow

## Purpose

This workflow defines the default development lifecycle Team Lead uses to plan, supervise, review, and close work performed by execution agents.

## Stages

1. Intake: confirm the request, scope, constraints, and missing information.
2. Planning: define execution phases, risks, dependencies, and owners.
3. Architecture: confirm solution boundaries, integration points, and notable tradeoffs.
4. Design: translate the approved plan into file-level or component-level work items.
5. Implementation: dispatch execution agents and monitor their progress.
6. Testing: review validation commands, results, and failed-path handling.
7. Review: compare output against acceptance criteria and request rework if needed.
8. Wrap-up: summarize what changed, what was validated, and what risks remain.

## Checkpoints

- Each stage must define required evidence before the next stage starts.
- `Architecture` and `Design` may be skipped only when Team Lead records why the skip is safe.
- Rework must state what is missing, what evidence is required, and what the next acceptance bar is.

## Rework Limits

- Execution rework loops must have an explicit cap.
- When the cap is exceeded, Team Lead must either request user input or fail the task.

## Parallel Work Rules

- Parallel execution may start only after planning is approved.
- Every parallel branch needs its own evidence trail and review outcome.
