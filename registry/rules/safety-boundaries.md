# Registry Safety Boundaries

## Allowed Writes

- Registry state and heartbeat metadata.
- Audit-friendly status changes tied to explicit agent identifiers.

## Forbidden Actions

- Writing to source repositories or shared task workspaces.
- Performing business-agent work.
- Silently rewriting agent identity or capability data.

## Escalation Triggers

- Duplicate or conflicting agent registration.
- Invalid heartbeat identity.
- Capability query that depends on missing or corrupted registry data.
