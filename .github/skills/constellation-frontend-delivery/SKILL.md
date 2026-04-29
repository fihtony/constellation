---
name: constellation-frontend-delivery
description: >
  Frontend delivery playbook for Constellation development agents. Use when Team Lead
  or Web Agent plans, implements, or reviews UI work across layout, state, accessibility,
  performance, and design fidelity. Inspired by frontend-focused skills in github/awesome-copilot.
user-invocable: false
---

# Frontend Delivery

## When To Apply

- UI pages, flows, or components driven by Jira requirements, Figma, or Google Stitch designs.
- Frontend bug fixes involving rendering, interaction, responsive behavior, or state handling.
- Review of screenshots, implementation evidence, and acceptance criteria for visual tasks.

## Build Rules

- Treat design fidelity as a requirement, not a suggestion. Match layout hierarchy, spacing rhythm, typography intent, color usage, and interaction affordances.
- Prefer clear component boundaries and predictable state flow over clever abstractions.
- Preserve responsive behavior from the first implementation pass. Desktop-only success is incomplete.
- Include empty, loading, and error states whenever the page depends on remote data or user actions.

## Quality Checklist

- Accessibility: semantic structure, keyboard reachability, focus visibility, and meaningful labels.
- Performance: avoid avoidable re-renders, oversized bundles, blocking assets, and layout shift.
- Consistency: follow the repository's existing component patterns, routing model, and styling system unless the task explicitly requires change.
- Evidence: tests and screenshots must verify the user-visible outcome, not only internal implementation details.

## Review Standards

- Reject UI changes that ignore supplied design context without an explicit, justified deviation.
- Reject fragile CSS or component logic that only works for one viewport, one data shape, or one interaction path.
- Reject missing visual verification for major UI work.