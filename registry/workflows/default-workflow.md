# Registry Default Workflow

## Purpose

This workflow defines how the Registry service validates registration requests, updates instance state, and answers capability queries.

## Stages

1. Validate Input: confirm the request shape and required identity fields.
2. Update or Query State: apply the registration, heartbeat, or query operation.
3. Verify Consistency: ensure the resulting registry state is coherent.
4. Report: return a structured response suitable for automation.

## Checkpoints

- Never accept a state mutation without an explicit agent identity.
- Never hide conflicting registrations or stale-instance errors.
