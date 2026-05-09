# UI Design Agent — Validation Policy

## Before Returning Design Context

1. Verify that the response from Figma/Stitch contains expected fields (`name`, `id`, and design data).
2. Verify that the payload size is within the `maxPayloadBytes` limit.
3. If the design file is empty or has no matching screen, fail with a descriptive error.

## Data Quality Checks

1. If a component has no properties or is missing a name, log a warning but continue.
2. If color tokens are empty, return an empty array (not an error).
3. If the thumbnail URL is unavailable, omit it rather than returning null.
