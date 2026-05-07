# Office Agent Default Workflow

## Overview

The Office Agent workflow is **fully LLM-driven**. The agentic runtime (connect-agent, copilot-cli,
or claude-code) decides every step based on available tools and the task prompt. Python code only
handles the A2A protocol boundary, permission enforcement, and lifecycle bookkeeping.

## Lifecycle

```
POST /message:send
  └─► _run_workflow() [background thread]
        ├─► Permission check
        ├─► configure_office_control_tools()   # wire lifecycle callbacks
        ├─► build_system_prompt_from_manifest()
        ├─► build_office_task_prompt()          # load office/prompts/tasks/process.md
        └─► runtime.run_agentic()              # LLM takes over from here
              ├─► list_local_dir / search_local_files
              ├─► read_local_file / run_local_command
              ├─► write_local_file / edit_local_file
              ├─► report_progress (timeline updates)
              └─► complete_current_task / fail_current_task
```

## Phases (LLM-driven, not Python-hardcoded)

### 1. Understand the Request
The LLM reads the task prompt, identifies the capability (`summarize`, `analyze`, or `organize`),
and determines what tools it needs. No Python branching — the LLM decides.

### 2. Explore Target Files
The LLM uses `list_local_dir`, `search_local_files`, and `run_local_command` to enumerate the target
paths and understand the file structure before reading anything.

### 3. Process Files
- **Summarize**: `read_local_file` for each document → produce `summary.md`
- **Analyze**: `read_local_file` + `run_local_command` for data inspection → produce `analysis.md`
- **Organize**: `list_local_dir` + `search_local_files` → write `organization-plan.json` → optionally execute with `run_local_command`

### 4. Write Output
The LLM writes results to `{workspace_path}/office-agent/` using `write_local_file`,
or in-place for INPLACE output mode.

### 5. Complete
The LLM calls `complete_current_task` with a text summary and artifact paths.
Python then sends the A2A callback to Compass with the final state and artifacts.

## Audit Files

Each run produces these files under `{workspace_path}/office-agent/`:

| File | Purpose |
|------|---------|
| `command-log.txt` | Timestamped log of major workflow milestones |
| `stage-summary.json` | Final task state, runtime config, turns used |
| `summary.md` / `analysis.md` / `organization-report.md` | Primary output |
| `organization-plan.json` | Organize plan (organize tasks only) |
| `failure.txt` | Error details (failed tasks only) |

## Error Handling

- Permission denied for a target path → task fails immediately with a permission error artifact.
- Unreadable file during processing → logged as warning, processing continues for other files.
- Runtime timeout → task fails with partial results in the summary artifact.
- All failures notify Compass via callback with `TASK_STATE_FAILED`.

## Rework Policy

The Office Agent does not have a review/rework cycle. Each task is single-pass.
If the result is unsatisfactory, the user submits a new task with refined instructions.

