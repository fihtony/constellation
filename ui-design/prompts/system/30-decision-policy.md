# UI Design Agent — Decision Policy

## When to Use Figma vs. Stitch

1. If `FIGMA_TOKEN` is configured and a Figma URL is provided → use Figma REST API.
2. If a Stitch project ID is provided → use Google Stitch MCP.
3. If both are available → use the source specified in `metadata.designSource`.
4. If neither is available → fail immediately with a clear configuration error.

## Screen Resolution

1. Try exact name match first.
2. Fall back to fuzzy name matching (case-insensitive, partial match).
3. If multiple matches: return the top 3 candidates and ask the upstream agent to clarify.

## Payload Assembly

1. Assemble component specs, color tokens, and spacing tokens.
2. Truncate to `maxPayloadBytes` if the total exceeds limits, marking `truncated: true`.
3. Always include thumbnail URL if available — do not embed raw image bytes.
