---
name: constellation-architecture-delivery
description: >
  Architecture playbook for Constellation development agents. Use when Team Lead or
  Web Agent needs to turn requirements into implementation boundaries, service/data
  flow decisions, scalability choices, or change-impact guidance. Inspired by the
  architecture-focused skills collected in github/awesome-copilot.
user-invocable: false
---

# Architecture Delivery

## When To Apply

- Before implementation planning for any feature that spans multiple modules, services, or external systems.
- When the task changes API contracts, data flow, persistence, or cross-agent orchestration.
- During review, to reject code that works locally but violates architectural boundaries or operational constraints.

## Planning Checklist

- Start from explicit inputs and outputs: user request, Jira acceptance criteria, design context, repo boundaries, and registered capabilities.
- Identify the smallest owning abstraction for the change: route/controller, workflow step, service, persistence adapter, or shared utility.
- Keep cross-agent boundaries explicit. Fetch external context through boundary agents or MCP integrations already defined by the system; do not bypass them.
- Preserve existing runtime constraints: audit artifacts, stage summaries, deterministic workspaces, and reviewability.
- Keep permission policy centralized. If SCM branch protection changes, update `common/permissions/*.json` and the design docs instead of hardcoding new branch names or prefixes in handlers.

## Decision Heuristics

- Prefer extending an existing boundary or workflow over inventing a parallel path.
- Keep contracts narrow and observable: explicit JSON fields, stable artifact names, and deterministic status transitions.
- If a change adds new information flow, specify where it is fetched, where it is persisted, and which agent owns the decision.
- Optimize for reversibility: choose small edits, local validation, and clear rollback points.
- For branch-protection decisions, prefer regex-configured protected-branch policy over string-prefix heuristics. The default protected set is `main`, `master`, `develop`, and `release/*`; everything else is a development branch unless policy says otherwise.

## Review Standards

- Reject hidden coupling across agents, implicit dependency on host state, or configuration that only works outside the orchestrated runtime.
- Reject design drift between plan, implementation evidence, and review summary.
- Require documentation updates whenever architecture, capability routing, or runtime behavior changes.