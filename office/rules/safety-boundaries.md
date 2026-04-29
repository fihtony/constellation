# Office Agent Safety Boundaries

## Path Security

- All read/write paths are normalized via `os.path.realpath()` before access.
- Every path is validated against the mounted input root (`/app/userdata`) or output root.
- Symbolic links that resolve outside the allowed root are rejected immediately.
- Paths containing `..` segments are rejected.
- Destinations in organize plans must be relative, non-empty, and within the output root.

## Resource Limits

| Limit | Default | Config Variable |
|-------|---------|-----------------|
| Single file size | 50 MB | `OFFICE_MAX_FILE_SIZE_MB` |
| Directory file count | 2000 | `OFFICE_MAX_DIR_FILE_COUNT` |
| Directory total size | 250 MB | `OFFICE_MAX_DIR_TOTAL_MB` |

When directory limits are exceeded, a preflight report is returned without processing.

## File Write Safety

- Default behavior: never overwrite existing files. Conflict avoidance uses timestamp suffixes.
- In-place organize: every executed step is logged to `.office-agent-manifest.json` for recovery.
- `operations-plan.json` is persisted to workspace BEFORE the first write action.
- Concurrent writes to the same directory root are rejected with a fail-fast error.

## LLM Output Safety

- Only structured JSON operation plans are accepted from the LLM.
- Action whitelist: `mkdir`, `write_text`, `write_fragment`. All other actions are rejected.
- `write_fragment` must reference a valid `fragment_id` from the pre-built inventory.
- No shell commands, file deletion, or arbitrary code execution from LLM output.

## Format Restrictions

- `.doc` and `.ppt` (legacy binary): not supported; return clear conversion instructions.
- `.xls`: best-effort support; failures logged as warnings, other files continue.
- Macro-enabled files (`.xlsm`, `.docm`): macros are not executed; data-only read mode.
- Scanned/OCR PDFs: not supported; return clear error message.
- Password-protected files: single-file task fails; folder task logs warning and continues.

## Container Isolation

- Office Agent does NOT mount the Docker socket.
- Only `/app/userdata` (user directory) and `/app/workspace` (output) are mounted.
- No network access to external services except Compass callback and Registry heartbeat.
