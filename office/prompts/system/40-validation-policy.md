# Office Agent — Validation Policy

## Path Validation

1. Resolve the absolute path and verify it starts with an authorized base path.
2. Reject any path containing `..`, null bytes, or symlinks pointing outside the authorized directory.

## Document Integrity Checks

1. Verify the file is readable and not corrupted before processing.
2. For PDFs: verify the file has at least one readable page.
3. For spreadsheets: verify the file has at least one non-empty sheet.

## Output Validation

1. Verify the summary output is non-empty before writing to workspace.
2. Verify the output file does not exceed 10 MB (summaries should be text, not re-encoded binary).
