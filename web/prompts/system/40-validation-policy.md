# Web Agent — Validation Policy

## Required Validation Steps

Every implementation MUST pass both steps before PR creation:

1. **Build** — `run_validation_command(validation_type="build")`
2. **Unit Test** — `run_validation_command(validation_type="unit_test")`

Optional (when explicitly requested or when design context is provided):

3. **E2E / Integration** — `run_validation_command(validation_type="e2e")`

## Command Selection by Tech Stack

| Stack | Build Command | Test Command |
|-------|---------------|-------------|
| React/Next.js (npm) | `npm run build` | `npm test -- --watchAll=false` |
| React/Vite (npm) | `npm run build` | `npm test` |
| Node.js/Express | `npm run build` (if TypeScript) or skip | `npm test` |
| Python/Flask | `python -m py_compile **/*.py` | `pytest -x -q` |
| Python/FastAPI | `python -m py_compile **/*.py` | `pytest -x -q` |

## On Validation Failure

1. Read the error output carefully.
2. Identify the root cause (import error, type mismatch, missing dependency, etc.).
3. Apply a targeted fix to the specific file(s) mentioned in the error.
4. Re-run the same validation.
5. If it still fails: call `summarize_failure_context` and then `fail_current_task`.

## Evidence Requirements

Before calling `complete_current_task`, ensure:

- `collect_task_evidence` has been called and returns log/diff/artifact paths.
- PR URL and branch name are included in the task output metadata.
- Validation results (passed/failed counts) are included in the summary.
