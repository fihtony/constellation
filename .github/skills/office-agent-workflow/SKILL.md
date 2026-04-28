# Skill: Office Agent Workflow

## Purpose

Use this skill when implementing, reviewing, or testing the Office Agent and the
Compass-side Office routing flow.

This skill covers:
- Compass agentic-runtime routing for all user-facing cases
- Office path extraction and clarification handling
- Output-mode selection (`workspace` vs `inplace`)
- Conditional write-permission confirmation
- Office Agent execution for summarize / analyze / organize
- Bounded organize-plan validation and safe file writes

---

## Core Workflow

1. Compass uses the shared agentic runtime to classify every incoming request.
2. Development work routes to `team-lead.task.analyze`.
3. Local document work routes to one of:
   - `office.document.summarize`
   - `office.folder.summarize`
   - `office.data.analyze`
   - `office.folder.organize`
4. If an office task does not include an absolute path, Compass enters `TASK_STATE_INPUT_REQUIRED`.
5. Once the path is known, Compass asks where output should go:
   - `workspace` → source bind is read-only
   - `inplace` → Compass asks for write confirmation before dispatch
6. Compass launches Office Agent with `extraBinds` and sends metadata:
   - `officeTargetPaths`
   - `officeOutputMode`
   - `officeInputRoot`
   - `officeWorkspacePath`
7. Office Agent executes the requested capability and calls back to Compass.

---

## Safety Rules

- Compass must not guess missing office paths.
- Compass must use the agentic runtime for routing, clarification interpretation,
  and final user summaries.
- Office Agent must never execute arbitrary shell commands from runtime output.
- Organize plans must be validated against a strict action allowlist before any writes:
  - `mkdir`
  - `copy_file`
  - `write_text`
  - `write_fragment`
- Destinations must always be relative to the approved output root.
- Default behavior preserves originals. MVP does not delete source files.
- `copy_file` source validation uses `os.path.realpath()` before checking the allowlist.

---

## Output Conflict Protection

- `_non_overwrite_path(path)` appends a compact timestamp suffix when the target file already exists.
- Workspace mode writes `summary.md` / `analysis.md` (no task-id suffix); inplace mode uses `summary-{task_id}.md` to avoid collisions.
- For organize, the `.office-agent-manifest.json` always reflects the final executed actions.

---

## Operations Plan Before Writes (§9.6)

- `_execute_organize` saves `operations-plan.json` to the audit dir **before** executing any action.
- In inplace mode each executed action is also logged to `command-log.txt` immediately for human recoverability.

---

## Directory Preflight Limits

- `_preflight_scan(paths)` counts files and total bytes without reading content.
- If `overFileCountLimit` or `overBytesLimit` is set, `_execute_summary` / `_execute_analysis` return a preflight report artifact instead of running the LLM, avoiding OOM.
- Hard limits are configurable via `OFFICE_MAX_FILE_SIZE_MB`, `OFFICE_MAX_DIR_FILE_COUNT`, `OFFICE_MAX_DIR_TOTAL_MB`.

---

## Container Security

- All agent containers run as non-root `appuser` (UID 1000); see each `Dockerfile`.
- Compass container needs `group_add: [docker]` in `docker-compose.yml` to access the Docker socket as non-root.

---

## Format Support

Guaranteed MVP support:
- `.txt`
- `.csv`
- `.xlsx`
- `.docx`
- `.pptx`
- `.pdf` (text PDFs only)

Best-effort:
- `.xls`

Rejected with explicit guidance:
- `.doc`
- `.ppt`
- scanned/OCR-only PDFs

---

## Key Files

- `compass/app.py`
- `compass/prompts.py`
- `office/app.py`
- `office/prompts.py`
- `common/launcher.py`
- `common/launcher_rancher.py`
- `tests/test_compass_dispatch.py`
- `tests/test_office_agent.py`

---

## Validation Commands

Run the focused unit tests first:

```bash
/Users/tony/projects/constellation/venv/bin/python -m unittest \
  tests.test_compass_dispatch \
  tests.test_office_agent \
  tests.test_env_isolation
```

Build the Office Agent image when validating container wiring:

```bash
./build-agents.sh office
```

---

## Common Failure Modes

- Office task stays in `TASK_STATE_INPUT_REQUIRED` because the path is not absolute.
- Office launch fails because `OFFICE_ALLOWED_BASE_PATHS` rejects the selected path.
- Office organize fails because runtime returned an unsafe destination such as `../...`.
- Workspace output is missing because `ARTIFACT_ROOT_HOST` was not configured for Compass.
- In-place mode was requested but the user denied write permission.
- `copy_file` action fails validation because the source path uses a symlink alias vs realpath — validate both paths agree under realpath normalization.
