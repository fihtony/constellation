# SCM Agent Safety Boundaries

## Allowed Actions

- Repository inspection.
- Scoped clone, branch, push, and pull request operations.
- Read-only file and tree inspection inside approved repositories.

## Forbidden Actions

- Force-push, branch deletion, or protected-branch mutation without explicit approval. Protected branches are defined by the task permission policy regex list, not hardcoded in the handler.
- Writing to repositories that were not explicitly selected.
- Hiding provider-side permission or policy failures.

## Escalation Triggers

- Ambiguous repository target.
- Protected-branch or required-review policy conflict.
- Requested action would rewrite published history.
