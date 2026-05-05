---
name: constellation-code-review-delivery
description: >
  Code review, security, and refactoring playbook for Constellation development agents.
  Use when Team Lead reviews Web Agent output, or when any agent needs to evaluate
  correctness, security posture, or code quality before accepting a deliverable.
  Inspired by security-review, review-and-refactor, and quality-playbook patterns
  in github/awesome-copilot.
user-invocable: false
---

# Code Review Delivery

## When To Apply

- Team Lead reviewing Web Agent implementation output before marking a task complete.
- Evaluating a pull request for correctness, security, edge-case coverage, and maintainability.
- Identifying regressions, missing error handling, or architectural violations in new code.
- Any review where the deliverable touches auth, input handling, external API calls, or data mutation.

## Review Process

Execute these passes in order. Each pass has a clear output.

### Pass 1 — Correctness

- Read every changed function body (not just signatures) against the acceptance criteria.
- Verify output values, status transitions, and returned artifacts match what was specified.
- Check that error paths return informative status rather than silently succeeding.
- Confirm external calls (Jira, SCM, Figma, database) are actually made and results are used.

### Pass 2 — Security (OWASP Top 10 Focus)

Check for the following in all changed code:

| Risk | What To Look For |
|------|-----------------|
| Injection (A03) | String interpolation into SQL, shell commands, or dynamic eval |
| Auth failures (A07) | Missing permission checks, JWT weaknesses, hardcoded credentials |
| Sensitive data (A02) | Secrets in logs, API responses, or error messages |
| SSRF (A10) | User-controlled URLs passed to `urlopen`, `requests.get`, etc. |
| Path traversal | User input in file paths without normalization |
| Insecure deserialization | `eval()`, `pickle.loads()`, or untrusted JSON schema coercion |

Flag any finding with severity: CRITICAL / HIGH / MEDIUM. Do not auto-apply fixes — flag for human review.

Additional Constellation-specific checks:

- Verify every boundary-agent read/write path enforces the task permission snapshot, including direct HTTP convenience endpoints.
- Reject compatibility fallbacks that silently allow missing or malformed permission snapshots in `strict` mode.
- For SCM changes, verify protected-branch logic comes from central regex policy (`common/permissions/*.json`) rather than scattered hardcoded branch names.

### Pass 3 — Edge Cases and Reliability

- What happens when required inputs are None, empty, or malformed?
- What happens when external services return 4xx/5xx, timeout, or partial data?
- What happens when file system operations fail (missing directory, permission denied)?
- Are partial-success cases handled or silently swallowed?

### Pass 4 — Test Coverage

- Are the critical paths covered by tests (unit and/or integration)?
- Do tests assert meaningful output values, not just that no exception was thrown?
- Are external dependencies mocked or isolated so tests are deterministic?

## Review Standards

- Require line numbers with every finding. No line number means no finding.
- Flag as QUESTION rather than BUG when context is ambiguous.
- Do not suggest style changes — only flag things that are incorrect, insecure, or incomplete.
- For every CRITICAL/HIGH security finding, propose a concrete fix alongside the finding.
- A deliverable passes review when: all acceptance criteria are demonstrably met, no CRITICAL/HIGH security issues remain, and key error paths have test coverage.
- Treat missing tests for protected-branch regex overrides, permission-header handling on GET endpoints, or fail-closed enforcement as review gaps, not optional follow-ups.

## Refactoring Guidance

When the review identifies code that should be refactored before merging:

- Keep the refactor scope minimal — change only what must change to fix the finding.
- Ensure existing tests still pass after any refactor.
- Preserve function signatures and artifact names unless the specification requires change.
- Prefer clarity and correctness over clever optimizations.
