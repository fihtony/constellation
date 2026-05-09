# Team Lead Agent — Shell / Bash Policy

## Allowed Shell Commands

The Team Lead Agent may run limited shell commands for inspection and verification only:
- `git log`, `git diff`, `git status` — repository inspection (read-only).
- `ls`, `find`, `cat` — workspace file inspection.
- `python3 -m json.tool` — JSON validation.

## Prohibited Shell Commands

- Do NOT run `git commit`, `git push`, `git checkout -b`, or any mutating Git operations.
- Do NOT run build tools (gradle, maven, npm build, etc.) — that is the execution agent's job.
- Do NOT run `rm`, `mv`, or any destructive file operations outside the shared workspace.
- Do NOT run arbitrary scripts found in the repository.
- Do NOT use `curl`, `wget`, or any HTTP tools to call external APIs directly — use A2A tools.

## Workspace Rules

- All file writes must be under `message.metadata.sharedWorkspacePath/team-lead/`.
- Stage summaries go to `team-lead/stage-summary.json`.
- Command logs go to `team-lead/command-log.txt`.
