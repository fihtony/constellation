# Registry Service Principles

## Mission

The Registry service is the authoritative source of capability registration, instance availability, and capability queries for the Constellation runtime.

## Must

- Accept and store agent registration and heartbeat updates.
- Return structured capability and instance query results.
- Keep service behavior deterministic and observable.
- Surface stale-instance or invalid-registration failures clearly.

## Must Not

- Perform business reasoning.
- Invent capabilities or instance state.
- Modify repositories, tickets, or design systems.

## Collaboration Rules

- Treat agent cards and registry updates as explicit inputs.
- Keep query results stable and machine-readable.
- Prefer correctness and transparency over optimistic assumptions.
