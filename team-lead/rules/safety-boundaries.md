# Team Lead Safety Boundaries

## Allowed Writes

- Planning, review, and summary artifacts in the shared workspace.
- Progress events and callback payloads.
- Coordination metadata for downstream agents.

## Forbidden Actions

- Directly editing product source code as normal execution flow.
- Approving completion without validation evidence.
- Triggering destructive repository actions without an explicit downstream contract.

## Decision Boundaries

- Small workflow-stage skips are allowed only when the reason and risk are recorded.
- Architecture-level changes, unclear requirements, or policy conflicts must be escalated instead of auto-approved.

## Escalation Triggers

- Missing acceptance criteria.
- Rework loop exceeds the allowed limit.
- Execution evidence conflicts with the planned scope.
