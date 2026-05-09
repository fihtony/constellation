# SCM Agent — Failure Handling

## Authentication Failures

- Report immediately with `fail_current_task`.
- Include: provider name, whether token was present, repo URL (without credentials).
- Never log the SCM token value.

## Protected Branch Violation

- Reject immediately with a clear error describing which branch is protected.
- Do NOT attempt to work around the protection.

## Repository Not Found

- Fail with `fail_current_task` and include the requested repo URL.
- Suggest checking repository permissions and URL correctness.

## Push Conflicts

- Report the conflict without attempting auto-resolution.
- Include: conflicting branch, base ref, conflicting files if available.

## Rate Limiting

- Retry up to 3 times with exponential backoff (2s, 4s, 8s).
- If still failing, fail with `rate_limit_exceeded` reason.
