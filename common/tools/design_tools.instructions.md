# Design Tools — Usage Guide

## Available Tools

### `design_fetch_figma_screen`
Fetch a Figma screen or component specification.

**When to use**: When the task references a Figma URL or screen name.

**What you receive**: Layout specifications, component hierarchy, colors, typography, spacing, and component properties.

### `design_fetch_stitch_screen`
Fetch a Stitch design specification.

**When to use**: When the task references a Stitch screen ID.

## Best Practices
- Fetch design specs before writing any UI code.
- Map Figma component names to existing component library components (e.g., Ant Design, Material UI) before creating new ones.
- If a design spec is unavailable, implement a reasonable approximation and note it in the PR description.
- Do not block implementation if design tools are unavailable — proceed with best judgment.
