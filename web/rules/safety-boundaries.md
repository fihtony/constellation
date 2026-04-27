# Web Agent Safety Boundaries

## Allowed Writes

- The assigned repository files.
- The shared workspace artifact directory for this task.
- Build or test outputs generated inside the approved workspace.

## Forbidden Actions

- Editing unrelated repositories or task workspaces.
- Writing secrets to source files, logs, or artifacts.
- Performing destructive git operations such as force-push or hard reset without explicit approval.

## Auto-Repair Boundaries

- Auto-repair is allowed for implementation, test, lint, and review issues within the approved scope.
- Auto-repair must stop when the issue is architectural, policy-related, or blocked by external credentials.

## Escalation Triggers

- Requirement ambiguity.
- Requested change exceeds the approved scope.
- Validation still fails after the allowed number of repair loops.
