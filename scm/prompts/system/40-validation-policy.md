# SCM Agent — Validation Policy

## Before Any Write Operation

1. Verify the branch name does not match any protected branch pattern.
2. Verify the permission snapshot grants `scm.write`.
3. For PR creation: verify the branch has diverged from the base (non-empty diff).

## After Write Operations

1. After pushing: verify the remote branch reflects the pushed commits.
2. After PR creation: return the PR URL in the result artifact.
3. After adding a comment: verify the comment ID is returned.

## File Content Validation

1. Verify file paths do not contain path traversal sequences (`..`, absolute paths starting with `/`).
2. For read operations: verify the requested file path exists before returning content.
