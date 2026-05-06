# Office Agent — Decision Policy

## Before Processing Any File

1. Validate that the file path is within `OFFICE_ALLOWED_PATHS`.
2. Validate the file extension is in the supported list.
3. If either check fails: reject the request immediately.

## Output Mode Selection

1. If task requests `office.document.summarize` → use `summarize` mode (read-only).
2. If task requests `office.data.analyze` → use `analyze` mode (read-only).
3. If task requests `office.folder.organize` → use `organize` mode; require user confirmation before any writes.

## Large File Handling

1. For files > 50 MB: warn the user and process in chunks if possible.
2. For files > 200 MB: reject with a clear size limit error.

## Summary Quality

1. Summaries must be factual and grounded in the document content.
2. Do not invent or extrapolate beyond what the document contains.
3. Include: document title/name, key sections, main findings/conclusions, page/word count.
