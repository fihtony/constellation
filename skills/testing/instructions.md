# Testing Strategy

## Test Pyramid
- Unit tests: majority (fast, isolated, deterministic)
- Integration tests: moderate (test component interactions)
- E2E tests: few (critical user workflows only)

## Before Running Tests (MANDATORY)

Always ensure dependencies are installed and the project builds:
1. Run `npm install` (or `pip install -e ".[dev]"` for Python) before running tests
2. Run `npm run build` to verify there are no compilation errors
3. Only then run the test suite

For Web Dev Agent work, this is a hard gate, not a recommendation. After any
code change or repair attempt, the agent must run the repository validation
script through the workflow node. The required order is always:
1. Install dependencies with `npm install`
2. Build with `npm run build`
3. Run the configured test command or supported runner

The agent must not run self-assessment, screenshot capture, Jira updates, or PR
creation until the validation script reports install, build, and tests all
passing. If validation fails, fix the local defect and rerun the same script.

If `npm install` fails:
- Read the error carefully — it usually names the missing/invalid package
- Remove the offending package from package.json
- Re-run `npm install` until it succeeds

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

## Frontend Tests (React / Vite)
- Use vitest + @testing-library/react + jsdom
- Configure jsdom in vite.config.js: `test: { environment: 'jsdom' }`
- Import `@testing-library/jest-dom` in test setup file
- Test rendered output, user interactions (click, input), and accessibility

## Test Naming Convention
- `test_<what>_<condition>_<expected>` or descriptive sentence
- Example: `test_create_session_with_valid_agent_returns_session`

## Coverage Target
- Minimum 80% line coverage for new code
- 100% coverage for critical paths (auth, payments, data mutations)
