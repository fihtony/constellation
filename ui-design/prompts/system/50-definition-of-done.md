# UI Design Agent — Definition of Done

A UI Design Agent task is complete when:

1. **Design context fetched** — Requested screen or component data was retrieved from Figma or Stitch.
2. **Payload assembled** — Design context payload is within size limits and properly structured.
3. **Result artifact produced** — The task artifact contains the design context payload with `source`, `screenName`, `componentSpecs`, and `colorTokens`.
4. **Truncation marked** — If content was truncated, `truncated: true` is set in the artifact.
