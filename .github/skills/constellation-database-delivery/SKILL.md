---
name: constellation-database-delivery
description: >
  Database delivery playbook for Constellation development agents. Use when Team Lead
  or Web Agent evaluates schema work, query behavior, migrations, indexing, data integrity,
  or persistence-facing review. Inspired by SQL and database-focused skills in github/awesome-copilot.
user-invocable: false
---

# Database Delivery

## When To Apply

- Schema evolution, migrations, query changes, repository/data-access edits, or performance-sensitive persistence work.
- Any feature that changes stored data shape, constraints, lifecycle, or reporting behavior.
- Review of SQL, ORM usage, indexing, or data consistency assumptions.

## Build Rules

- Model the data change explicitly: current state, target state, migration path, and rollback path.
- Prefer additive, reversible migrations unless destructive changes are explicitly justified.
- Keep queries correct before optimizing them; then remove obvious scans, N+1 patterns, and unnecessary round trips.
- Enforce integrity in the data model or persistence boundary, not only in UI code.

## Safety Checklist

- Consider nullability, defaults, uniqueness, foreign-key relationships, and backfill behavior.
- Verify the effect on existing reads and writes, not just the new happy path.
- For performance-sensitive changes, document expected indexes, query filters, and sort paths.
- Include tests that prove the persistence behavior or query contract required by the feature.

## Review Standards

- Reject schema changes with no migration story.
- Reject query changes that are hard to reason about, unbounded by default, or likely to regress under production-sized data.
- Reject persistence logic that leaks storage details across unrelated layers.