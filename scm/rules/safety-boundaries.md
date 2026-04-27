# SCM Agent Safety Boundaries

## Allowed Actions

- Repository inspection.
- Scoped clone, branch, push, and pull request operations.
- Read-only file and tree inspection inside approved repositories.

## Forbidden Actions

- Force-push, branch deletion, or default-branch mutation without explicit approval.
- Writing to repositories that were not explicitly selected.
- Hiding provider-side permission or policy failures.

## Escalation Triggers

- Ambiguous repository target.
- Protected-branch or required-review policy conflict.
- Requested action would rewrite published history.
