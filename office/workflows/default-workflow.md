# Office Agent Default Workflow

## Phases

### 1. Preflight

- Validate `requestedCapability` and `officeTargetPaths` from message metadata.
- Verify all target paths are within the mounted input root.
- Run directory-level resource scan (`_preflight_scan`): file count, total bytes, oversized files.
- If limits are exceeded, return a preflight report artifact and stop.

**Exit criteria:** All paths valid, within limits.

### 2. File Collection and Extraction

- Walk target paths and collect supported files.
- Skip oversized files with a warning.
- For summarize: extract text preview from each document.
- For analyze: build statistical profiles (CSV profile, workbook profile).
- For organize: build inventory + extract text fragments from `.txt` files.

**Exit criteria:** At least one readable file/profile collected.

### 3. LLM Processing

- Send collected data to Agentic Runtime with capability-specific prompt.
- For summarize/analyze: receive `summary_markdown` response.
- For organize: receive structured `actions` plan.
- Parse and validate LLM JSON response.

**Exit criteria:** Valid structured response received.

### 4. Plan Validation (Organize only)

- Validate all actions against the whitelist (`mkdir`, `write_text`, `write_fragment`).
- Canonicalize all destination paths (strip wrapper directories, enforce `files/` root).
- Verify `write_fragment` references against the inventory.
- Persist `operations-plan.json` to workspace BEFORE any writes.

**Exit criteria:** All actions pass validation; plan persisted.

### 5. Execution

- For summarize/analyze: write Markdown report to output location.
- For organize: execute actions sequentially; update manifest after each step.
- Apply conflict-avoidance (timestamp suffix) for existing files.
- For in-place mode: append per-step progress to `command-log.txt`.

**Exit criteria:** All actions executed or failed gracefully.

### 6. Completion

- Write `warnings.md` if any warnings accumulated.
- Update `stage-summary.json` with final phase and runtime config.
- Notify Compass via callback URL with final state and artifacts.

**Exit criteria:** Callback sent, task state set to COMPLETED or FAILED.

## Error Handling

- If any phase fails, task transitions to `TASK_STATE_FAILED`.
- Partial success in folder tasks: completed files produce output, failed files logged in `warnings.md`.
- Disk space or write errors during organize: stop further writes, preserve manifest for recovery.
- Runtime timeout: stop processing, return error summary with partial results if available.

## Return Work Limits

Office Agent does not have a review/rework cycle. Each task is single-pass.
If the result is unsatisfactory, the user submits a new task with refined instructions.
