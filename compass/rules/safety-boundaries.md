# Compass Safety Boundaries

## Allowed Writes

- Compass task state.
- Compass-managed artifact metadata.
- User-visible progress and callback records.

## Forbidden Actions

- Writing to source repositories.
- Running development commands inside shared workspaces.
- Performing direct Jira, SCM, or design-system mutations as a substitute for the proper agent.

## Data Handling

- Expose only the minimum user-facing detail needed for the current response.
- Do not leak secrets, tokens, or internal-only logs.

## Escalation Triggers

- Missing Team Lead capability.
- Inconsistent downstream task state.
- Resume request with missing or mismatched task context.
