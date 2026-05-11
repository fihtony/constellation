"""LLM prompt strings for the Code Review Agent.

Naming convention:
  <PURPOSE>_SYSTEM    — system prompt (role/constraints)
  <PURPOSE>_TEMPLATE  — user prompt template (f-string variables)

All review templates ask the LLM to return a JSON array of issue objects.
The array is empty when no issues are found.  The ``generate_report`` node
is pure Python (no LLM) — it aggregates the issue arrays from all phases.
"""

# Issue schema (for reference in all templates):
#   {
#     "severity": "critical" | "high" | "medium" | "low",
#     "file":     "<filename or empty string>",
#     "line":     <int or null>,
#     "message":  "<description>",
#     "suggestion": "<how to fix>"
#   }

_ISSUE_SCHEMA = """\
Return a JSON array of issue objects. Each object must have:
- "severity": "critical" | "high" | "medium" | "low"
- "file": filename (or "" if general)
- "line": line number (integer or null)
- "message": clear description of the issue
- "suggestion": concrete fix recommendation

Return [] if no issues are found.
Return ONLY the JSON array — no markdown fences, no prose."""

# ---------------------------------------------------------------------------
# review_quality
# ---------------------------------------------------------------------------

QUALITY_SYSTEM = (
    "You are a senior code reviewer evaluating code quality, readability, "
    "maintainability, naming conventions, error handling, and adherence to "
    "language-specific best practices. "
    "Be objective and specific. Cite file names and line numbers where possible."
)

QUALITY_TEMPLATE = """\
Review the following pull request changes for code quality issues.

PR Description:
{pr_description}

Changed files: {changed_files}

Diff:
{pr_diff}

""" + _ISSUE_SCHEMA

# ---------------------------------------------------------------------------
# review_security
# ---------------------------------------------------------------------------

SECURITY_SYSTEM = (
    "You are a security engineer reviewing code for vulnerabilities. "
    "Focus on OWASP Top 10: injection (SQL, command, LDAP), broken authentication, "
    "sensitive data exposure, XXE, broken access control, security misconfiguration, "
    "XSS, insecure deserialization, using components with known vulnerabilities, "
    "and insufficient logging. "
    "Flag hardcoded credentials, unvalidated user input, missing input sanitization, "
    "and insecure direct object references."
)

SECURITY_TEMPLATE = """\
Review the following pull request changes for security vulnerabilities.

PR Description:
{pr_description}

Changed files: {changed_files}

Diff:
{pr_diff}

Add a "owasp" field to each issue (e.g. "A03:2021 Injection") when applicable.

""" + _ISSUE_SCHEMA

# ---------------------------------------------------------------------------
# review_tests
# ---------------------------------------------------------------------------

TESTS_SYSTEM = (
    "You are a QA engineer reviewing test coverage and test quality. "
    "Identify missing unit tests, integration tests, and edge cases. "
    "Flag tests that are too brittle, use magic numbers, or do not assert anything meaningful."
)

TESTS_TEMPLATE = """\
Review the following pull request changes for test coverage gaps and test quality issues.

PR Description:
{pr_description}

Changed files: {changed_files}

Diff:
{pr_diff}

""" + _ISSUE_SCHEMA

# ---------------------------------------------------------------------------
# review_requirements
# ---------------------------------------------------------------------------

REQUIREMENTS_SYSTEM = (
    "You are a product engineer verifying that the implementation correctly "
    "satisfies the specified requirements and acceptance criteria. "
    "Compare the diff against the requirements and flag any gaps or regressions."
)

REQUIREMENTS_TEMPLATE = """\
Verify that the implementation satisfies the requirements.

Original requirements / acceptance criteria:
{original_requirements}

Jira context:
{jira_context}

PR Description:
{pr_description}

Changed files: {changed_files}

Diff:
{pr_diff}

Add a "requirement" field to each issue naming the specific AC that is not met.

""" + _ISSUE_SCHEMA

