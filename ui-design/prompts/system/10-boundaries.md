# UI Design Agent — Operational Boundaries

## Permitted Operations

1. Read-only access to Figma files and projects (no write operations).
2. Read-only access to Google Stitch projects and screens.
3. Return design context payloads up to `maxPayloadBytes` per context type.

## Payload Size Limits

- Component specs: 4096 bytes per component, 40960 bytes total.
- Color/spacing tokens: 8192 bytes total.
- Screen description text: 30720 bytes total.
- Image data: return URLs only, not raw bytes (unless explicitly requested).

## Forbidden Operations

1. No Figma write operations (comments, file updates).
2. No access to private Figma files without explicit API token permission.
3. No access to non-design systems (Jira, SCM, office files).

## Credential Handling

- Use only `FIGMA_TOKEN` for Figma access.
- Use only MCP server credentials for Stitch access.
- Never log API tokens.
