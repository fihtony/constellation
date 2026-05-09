# SCM Agent — Definition of Done

An SCM Agent task is complete when:

1. **Requested operation completed** — The SCM API confirmed the operation succeeded.
2. **Result artifact produced** — The task artifact contains structured results (PR URL, branch name, file content, etc.).
3. **No protected branch violations** — All operations were performed on non-protected branches.
4. **Audit trail present** — Write operations are logged with timestamps and operation details.
