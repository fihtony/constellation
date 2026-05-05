---
name: constellation-testing-delivery
description: >
  Testing strategy playbook for Constellation development agents. Use when Team Lead
  or Web Agent plans, writes, or evaluates tests for any deliverable — unit, integration,
  or end-to-end. Inspired by webapp-testing, playwright-generate-test, pytest-coverage,
  javascript-typescript-jest, and breakdown-test patterns in github/awesome-copilot.
user-invocable: false
---

# Testing Delivery

## When To Apply

- Before marking any implementation complete: verify test coverage exists for key behaviors.
- When writing tests for a new feature, bug fix, or refactored module.
- Evaluating whether existing tests actually cover the acceptance criteria, not just exercising code paths.

## Testing Strategy

### Tier 1 — Unit Tests (Required for all business logic)

- Test each function or module in isolation with controlled inputs.
- Cover: happy path, empty/None inputs, boundary values, error/exception paths.
- Mock external dependencies (HTTP calls, file I/O, databases) so tests are fast and deterministic.
- Assert meaningful output values — not just that no exception was raised.

### Tier 2 — Integration Tests (Required for API endpoints and external interactions)

- Test the full request-response cycle for each API endpoint: 200/201, 400, 404, 500.
- For external service calls (Jira, SCM, Figma), verify the agent correctly handles success, partial data, and error responses.
- Integration tests may use real services with test credentials or a reliable local mock (not random/flaky stubs).
- For permission-gated boundary endpoints, include explicit tests for: missing snapshot → 403, malformed snapshot → 403, allowed operation → success, denied operation → 403, and direct HTTP GET endpoints carrying `X-Task-Permissions`.

### Tier 3 — End-to-End / Acceptance Tests (Required for user-visible flows)

- For UI flows: verify the rendered output matches the acceptance criteria, not just that the page loads.
- For multi-step workflows: verify state transitions, artifact production, and callback/completion behavior.
- Capture evidence (screenshots, log excerpts, artifact contents) that can be included in the PR.

## Test Writing Rules

- Name tests to describe what they verify: `test_create_ticket_returns_id_on_success`, not `test_jira_1`.
- Parameterize tests across multiple input variants when the same logic handles different cases.
- Do not pad tests with trivial assertions (`assert isinstance(x, list)`) that inflate counts without catching bugs.
- Use `pytest` for Python code and follow the existing test runner patterns in the repository.
- Every test must import and call actual project code — no placeholder `pass` bodies.
- Any test that needs a Jira ticket URL/key, GitHub or Bitbucket repo URL, Figma URL, or Stitch URL must load that target from `tests/.env` (directly or through a helper). Do not hardcode real ticket IDs, repo URLs, or design URLs in test scripts.
- When branch authorization depends on policy, add at least one test for default protected branches (`main`, `master`, `develop`, `release/*`) and one test for a custom regex override.

## Coverage Targets

| Layer | Target | Rationale |
|-------|--------|-----------|
| Core business logic | ≥ 80% | Most bugs live here; coverage is achievable |
| API / endpoint handlers | All endpoints covered | Missing endpoint test means untested deployment risk |
| External service clients | All adapters have mock-based tests | External failures must be handled, not just hoped away |
| UI critical paths | Key user flows covered | Acceptance criteria are user-visible, not code-internal |

## Test Quality Checklist

- [ ] Tests assert output values, not just execution without exception.
- [ ] External services are mocked or isolated — tests pass without network access.
- [ ] Each acceptance criterion maps to at least one test that could fail if the criterion is not met.
- [ ] Error paths (invalid input, missing resource, service failure) have at least one test each.
- [ ] Test file runs cleanly with `pytest -v` (or equivalent) from the project root.
- [ ] A2A integration tests use `message.metadata.permissions`, and any retained direct HTTP convenience-endpoint tests use the matching debug transport (`X-Task-Permissions`) only for those non-production paths.

## Review Standard for Tests

- Reject test files that contain no assertions, or only `assert True`.
- Reject tests that depend on live external services unless they are explicitly labelled as integration tests and skipped in CI without credentials.
- Require that new features arrive with tests — tests are not an optional follow-up.
