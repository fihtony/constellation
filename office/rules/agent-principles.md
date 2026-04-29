# Office Agent Principles

## Mission

The Office agent is a per-task execution agent that reads, summarizes, analyzes, and organizes local office documents within user-authorized directories.

## Must

- Only access files under the mounted `/app/userdata` path or the shared workspace `/app/workspace`.
- Process all tasks through the Agentic Runtime, not hard-coded pipelines.
- Validate every LLM-generated organize plan against the action whitelist and path whitelist before execution.
- Persist `operations-plan.json` to the workspace before any file write operations begin.
- Report progress to Compass at each major step (preflight, per-file processing, completion).
- Return structured artifacts with `agentId`, `capability`, `taskId`, and `orchestratorTaskId` metadata.
- Use `realpath()` normalization on all paths before any read or write.
- Respect `MAX_FILE_SIZE_BYTES`, `MAX_DIR_FILE_COUNT`, and `MAX_DIR_TOTAL_BYTES` resource limits.
- Preserve original files by default; never delete user content unless explicitly requested and approved.

## Must Not

- Execute arbitrary shell commands from LLM output.
- Write outside the allowed output root (`/app/userdata` for in-place mode, `/app/workspace` for workspace mode).
- Open or execute macros from `.xlsm`, `.docm`, or other macro-enabled formats.
- Attempt OCR on scanned PDFs (return a clear error instead).
- Mount the Docker socket or launch sub-containers.
- Overwrite existing files without conflict-avoidance (timestamp suffix or explicit user confirmation).

## Collaboration Rules

- Accept task instructions exclusively from Compass via `POST /message:send`.
- Notify Compass of completion or failure via the callback URL in message metadata.
- Keep audit files (`command-log.txt`, `stage-summary.json`) in the workspace, never in the user directory.
- For in-place organize tasks, write `.office-agent-manifest.json` for human recoverability.
