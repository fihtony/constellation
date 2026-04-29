# SCM Agent Default Workflow

## Purpose

This workflow defines how the SCM agent resolves explicit repository requests, performs scoped SCM actions, and returns normalized repository results.

## Stages

1. Validate Input: confirm repository reference and requested action.
2. Inspect State: read repository, branch, or pull request state as needed.
3. Execute Action: perform the approved SCM operation.
4. Verify Result: confirm the target repository reflects the intended state.
5. Report: return structured identifiers, URLs, and failure categories.

## Checkpoints

- Never perform a write action without an explicit target repository.
- Never report completion without the resulting branch, PR, or clone state.
