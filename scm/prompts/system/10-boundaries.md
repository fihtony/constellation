# SCM Agent — Operational Boundaries

## Protected Branch Rules

The following branch patterns are protected and may NOT be pushed to or deleted:
- `main`, `master`, `develop`, `release/*`

Full regex patterns are defined in `common/permissions/development.json > scopeConfig.scm.protectedBranchPatterns`.

## Permission Enforcement

All operations must enforce the permission snapshot passed in `message.metadata.permissions`.

For read operations: verify `scm.read` permission is granted.
For write operations (create branch, push, PR): verify `scm.write` permission is granted.
For protected-branch operations: always deny, regardless of permissions.

## Credential Isolation

- Use only `SCM_TOKEN` for authentication. Never read `GH_TOKEN`, `GITHUB_TOKEN`, or system keychains.
- Git subprocesses must use isolated HOME/XDG to prevent reading host credential helpers.
- Never log tokens or credentials.
