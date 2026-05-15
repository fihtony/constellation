# Code Review

## Review Checklist

### Correctness
- Does the code implement the requirements correctly?
- Are edge cases handled?
- Are error paths covered?

### Security (OWASP Top 10)
- Input validation at system boundaries
- No SQL injection (parameterized queries)
- No XSS (output encoding)
- No hardcoded secrets or credentials
- Proper authentication and authorization checks

### Code Quality
- DRY — no duplicated logic
- Single Responsibility — each function does one thing
- Clear naming — variables and functions describe their purpose
- No dead code or commented-out code

### Testing
- Are there tests for the new/changed code?
- Do tests cover both happy path and error cases?
- Are tests deterministic (no flaky tests)?

### Performance
- No N+1 queries
- No unnecessary re-renders (React)
- Proper use of caching where applicable

## Review Output Format

Return a structured review with:
- verdict: "approved" | "needs_revision"
- issues: list of {severity, file, line, description, suggestion}
- summary: one-paragraph overall assessment
