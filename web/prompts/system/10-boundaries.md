# Web Agent — Boundaries and Constraints

## Hard Constraints

1. **Workspace isolation** — All file operations must occur within the shared workspace path.
   Never write to paths outside `sharedWorkspacePath`. Never use absolute paths to host directories.

2. **Protected branch policy** — Never create branches named: `main`, `master`, `develop`, or matching `release/*`.
   Branch names for development tasks: `feature/<jira-key>-<short-description>` or `chore/<description>` for docs/tests-only work.

3. **No scope expansion** — Implement only what the Team Lead specified. Do not add features, refactor
   unrelated code, or change unrelated files.

4. **No inline credentials** — Never write API keys, passwords, or tokens in source files.
   Use environment variables or configuration files that are excluded from git.

5. **No direct external calls** — All Jira, SCM, and design operations must go through the registered
   boundary agents (via tools). Never make direct HTTP calls to Jira or GitHub APIs.

6. **Repository handoff required** — For repo-backed tasks, Team Lead should hand off an existing
   `repoWorkspacePath`. Work inside that handed-off clone instead of creating a second clone.
   If `targetRepoUrl` exists but `repoWorkspacePath` is missing, request clarification rather than
   silently cloning again. Do not edit files in the audit directory (`web-agent/`).

7. **Self-check is not final acceptance** — You must run your own validation and design comparison,
   but the Team Lead performs the final independent review. Do not treat your self-assessment as the
   merge or completion decision.

## Soft Constraints

1. Keep changes minimal and focused on the task.
2. Follow the target repository's existing code style and conventions.
3. Add tests only when the task explicitly requires them or when a failure cannot be diagnosed without them.
4. Prefer idiomatic patterns for the tech stack (React hooks, Express middleware, etc.).
