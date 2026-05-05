---
name: constellation-backend-delivery
description: >
  Backend delivery playbook for Constellation development agents. Use when Team Lead
  or Web Agent plans, implements, or reviews server-side logic, APIs, integrations,
  background work, validation, and operational behavior. Inspired by backend and API
  guidance patterns in github/awesome-copilot.
user-invocable: false
---

# Backend Delivery

## When To Apply

- API endpoints, workflow handlers, background jobs, service adapters, or integration code.
- Any task that changes validation rules, persistence behavior, or external system interactions.
- Review of error handling, security boundaries, or test coverage for backend changes.

## Build Rules

- Make contracts explicit: request shape, response shape, status transitions, and failure modes.
- Validate inputs at the boundary and fail with actionable messages.
- Keep orchestration separate from side-effect execution where practical.
- Prefer idempotent operations and deterministic retries when workflows touch external systems.
- When Team Lead has already fetched Jira or design context, pass bounded copies downstream through A2A metadata instead of forcing the dev agent to re-fetch the same payload. Downstream agents should consume `jiraContext` / `designContext` first and call boundary agents again only for incremental detail.

## Security And Reliability

- Never assume upstream data is complete or trustworthy.
- Preserve auth, permission, and tenant boundaries already enforced by the system.
- Capture operational evidence for important side effects so Team Lead and Compass can review outcomes.
- Avoid silent fallbacks that hide degraded behavior; surface warnings in summaries or artifacts.
- Fail closed on permission input. Missing or malformed permission snapshots must reject boundary reads and writes in `strict` mode; do not preserve compatibility shortcuts in pre-release code.
- For SCM branch authorization, derive protected branches from regex-configured policy, not hardcoded names spread across handlers.

## Review Standards

- Reject business logic embedded in transport or HTTP glue when there is an existing service layer.
- Reject incomplete edge-case coverage around invalid input, not-found resources, external failures, and partial completion.
- Reject changes that mutate multiple systems without traceable evidence or rollback awareness.