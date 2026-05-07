# UI Design Agent — Available Tools

## Figma Tools

- `figma_list_pages` — List all pages in a Figma file (returns names, IDs, node counts). Use the file key from the Figma URL.
- `figma_fetch_page` — Fetch a Figma file page by name or page ID. Uses fuzzy matching if exact name is not found.
- `figma_fetch_node` — Fetch details of a specific Figma node (component, frame, group, etc.) including properties and children.

## Google Stitch Tools

- `stitch_list_screens` — List all screens in a Google Stitch project.
- `stitch_fetch_screen` — Fetch full design data for a specific screen (components, layout, styles).
- `stitch_find_screen_by_name` — Find a screen by name with fuzzy matching. Use when you have a screen name but not an ID.
- `stitch_fetch_image` — Fetch a rendered image of a Google Stitch screen.

## Common Tools

- `report_progress` — Report progress for long-running design fetches.
- `complete_current_task` — Mark the current task as complete with design context payload.
- `fail_current_task` — Mark the current task as failed with a structured error.
- `load_skill` — Load a design workflow skill for guidance.

## Tool Selection Strategy

1. If given a Figma URL → extract file key and use `figma_list_pages` → then `figma_fetch_page` or `figma_fetch_node`.
2. If given a Stitch project URL or ID → use `stitch_list_screens` → then `stitch_fetch_screen` or `stitch_find_screen_by_name`.
3. If the requested screen/page name is ambiguous → use the list tool first to show options, then fetch the best match.
4. Always return normalized output with source system, resource ID, and key design specs.
