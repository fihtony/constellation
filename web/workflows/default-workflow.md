# Web Agent Default Workflow

## Purpose

This workflow defines how the Web agent receives scoped implementation work, executes it, validates it, and returns structured evidence.

## Stages

1. Intake: read the task brief, acceptance criteria, and current workflow stage.
2. Inspect: identify the smallest relevant code surface and dependencies.
3. Implement: make the required code changes within the approved scope.
4. Validate: run the narrowest relevant build, test, lint, or typecheck command.
5. Repair: if validation fails, run a bounded self-repair loop and validate again.
6. Report: persist artifacts and return a concise summary to Team Lead.

## Checkpoints

- The first meaningful code change must be followed by focused validation.
- Evidence must include the actual command used and its result.
- Rework responses must explicitly address Team Lead feedback.

## Failure Handling

- Escalate immediately for architecture conflicts, requirement conflicts, or external-system permission failures.
- Stop auto-repair after the allowed limit and report the full local defect summary.
