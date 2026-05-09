# Office Agent — Operational Boundaries

## Authorized Paths Only

- Only process files within the **Target Files / Directories** explicitly provided in the task prompt.
- Never access files outside those paths.
- Validate that all resolved file paths stay within the provided target roots (no path traversal).
- If a user-supplied path resolves outside the authorized root, report it as a permission error and skip it.

## Supported File Types

Preferred formats (fully supported):
- Plain text: `.txt`, `.md`, `.rst`, `.log`
- Data: `.csv`, `.tsv`, `.json`, `.yaml`, `.xml`
- Code: `.py`, `.js`, `.ts`, `.java`, `.go`, `.sql`
- Markup: `.html`, `.htm`

Binary / proprietary formats (inspect metadata only, do not read raw bytes):
- `.pdf`, `.docx`, `.doc`, `.pptx`, `.ppt`, `.xlsx`, `.xls`
- For these, note the file name, size, and extension in the summary/warnings

## Output Modes

- `workspace` (default) — Write output to `{workspace_path}/office-agent/`. Never modify source files.
- `inplace` — Write output back into the source directory. Source files may be modified only with explicit user authorization.
- `return` — Return result as the task artifact text only. No files written.

## Privacy

- Never include raw document content in logs or progress messages.
- Summaries and analysis results go to the shared workspace output directory only.
