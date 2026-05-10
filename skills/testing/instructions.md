# Testing Strategy

## Test Pyramid
- Unit tests: majority (fast, isolated, deterministic)
- Integration tests: moderate (test component interactions)
- E2E tests: few (critical user workflows only)

## Unit Tests
- Test one unit of behavior per test
- Use descriptive test names that explain what is being tested
- Follow AAA pattern: Arrange, Act, Assert
- Mock external dependencies, not internal implementation
- Test edge cases: empty input, null, boundary values

## Integration Tests
- Test real interactions between components
- Use in-memory databases for data layer tests
- Use mock HTTP servers for external API tests
- Verify request/response contracts

## Test Naming Convention
- `test_<what>_<condition>_<expected>` or descriptive sentence
- Example: `test_create_session_with_valid_agent_returns_session`

## Coverage Target
- Minimum 80% line coverage for new code
- 100% coverage for critical paths (auth, payments, data mutations)
