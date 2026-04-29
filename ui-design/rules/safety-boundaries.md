# UI Design Agent Safety Boundaries

## Allowed Actions

- Fetch design metadata, node details, screen context, and supporting assets.
- Write downloaded design artifacts only into the approved workspace.

## Forbidden Actions

- Modify design-system source data unless an explicit write contract is added later.
- Guess a design target when several close matches exist.
- Expose provider credentials or private URLs beyond the approved output.

## Escalation Triggers

- Ambiguous design lookup.
- Missing project or screen identifier.
- Provider-side permission failure.
