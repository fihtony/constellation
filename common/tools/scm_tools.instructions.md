# SCM Tools — Usage Guide

## Available Tools

### `scm_create_branch`
Create a new feature branch. Call before making any code changes.

**Naming convention**: `feature/<ticket-key>-<short-description>` or `fix/<ticket-key>-<description>`.

### `scm_push_files`
Push modified files to the remote branch.

**When to use**: After all files are implemented and the build/tests pass locally.

**Constraints**:
- Do not push broken code. Run build and tests first.
- Include a meaningful commit message describing what changed and why.
- Limit each push to a logical unit of work.

### `scm_create_pr`
Create a pull request.

**When to use**: After pushing all changes and verifying the build passes.

**PR body template**:
```
## Summary
- <bullet: what changed>
- <bullet: why>

## Test plan
- [ ] Build passes
- [ ] Unit tests pass
- [ ] Manual verification: <describe>
```

## Order of Operations
1. `scm_create_branch` → create branch
2. Implement changes (Bash, Read, Write, Edit)
3. Run build and tests (Bash)
4. Fix any failures
5. `scm_push_files` → push changes
6. `scm_create_pr` → open PR
