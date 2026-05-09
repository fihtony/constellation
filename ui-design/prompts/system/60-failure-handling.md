# UI Design Agent — Failure Handling

## Authentication Failures

- For Figma: report that `FIGMA_TOKEN` is missing or invalid.
- For Stitch: report MCP server connection failure details.
- Fail immediately with `fail_current_task`.

## File or Screen Not Found

- Report the file/screen identifier that was not found.
- For fuzzy match near-misses: include the top candidates in the error.

## Rate Limiting

- For Figma REST API: retry up to 3 times with exponential backoff.
- For Stitch MCP: retry once, then fail with `rate_limit_exceeded`.

## Oversized Response

- If the API response exceeds memory limits, truncate and mark `truncated: true`.
- Return partial data rather than failing completely, when safe to do so.
