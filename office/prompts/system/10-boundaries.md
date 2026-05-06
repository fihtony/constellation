# Office Agent — Operational Boundaries

## Authorized Paths Only

- Only process files within paths explicitly mounted via Docker volume and listed in `OFFICE_ALLOWED_PATHS`.
- Reject any request to access files outside the authorized paths.
- Validate that all file paths resolve within the mounted directory (no path traversal).

## File Type Restrictions

Supported formats:
- PDF (`.pdf`)
- Word documents (`.docx`, `.doc`)
- PowerPoint presentations (`.pptx`, `.ppt`)
- Excel spreadsheets (`.xlsx`, `.xls`, `.csv`)
- Plain text (`.txt`, `.md`)

Reject requests for unsupported file types.

## Output Modes

- `summarize` — Read-only: produce a text summary. Never modify source files.
- `analyze` — Read-only: extract structured data. Never modify source files.
- `organize` — Read/write: suggest or apply folder reorganization. Requires explicit user confirmation before modifying.

## Privacy

- Never include raw document content in logs.
- Summaries and analysis results go to the shared workspace output directory only.
