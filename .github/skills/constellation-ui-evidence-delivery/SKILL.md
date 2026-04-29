# Constellation UI Evidence Delivery

Adapted from [web-design-reviewer](https://github.com/github/awesome-copilot/tree/main/skills/web-design-reviewer)
in the github/awesome-copilot collection.

This skill guides the Web Agent when capturing visual evidence of UI implementations
and comparing them against the design reference. It defines viewport standards,
screenshot naming conventions, and the structured evidence report expected in every
PR that includes a UI component.

---

## When to Apply

Apply whenever a completed task includes any of:
- HTML templates rendered by a web framework (Flask, Django, FastAPI, etc.)
- A React, Vue, Next.js, Nuxt, or similar frontend
- A static site (plain HTML/CSS/JS)
- A fullstack implementation with at least one rendered page

Backend-only tasks (REST APIs, workers, scripts) do **not** require UI screenshots.

---

## Evidence File Layout

All visual evidence lands under `docs/evidence/` in the repository:

| File | Description |
|------|-------------|
| `docs/evidence/design-reference.png` | Screenshot of the Stitch/Figma design screen (design spec) |
| `docs/evidence/screenshot-{W}x{H}.png` | Implementation screenshot at viewport `{W}×{H}` |

**Screenshot naming rule**: filenames encode only the viewport dimensions.
Never use platform labels such as "desktop", "mobile", or "tablet" —
these tie the evidence to device categories that may not apply universally.

### Standard Viewports

| Width × Height | Represents |
|---------------|------------|
| 1280 × 900 | Laptop / standard desktop |
| 375 × 812 | Smartphone (iPhone-class) |

Additional viewports (e.g., 768×1024 for tablet) may be added by updating
`_UI_SCREENSHOT_VIEWPORTS` in `web/app.py`.

---

## Capture Procedure

1. **Start the application locally** on a free port using a headless subprocess.
   - For Flask/FastAPI: `python -m flask run --host=127.0.0.1 --port={PORT}`
   - For Node.js SPA: `npm run preview -- --port {PORT}` or `npm start`
   - Wait up to 20 s for the server to respond to a health probe.

2. **Capture each viewport** with headless Chromium:
   ```
   chromium --headless --no-sandbox --disable-gpu --disable-dev-shm-usage \
     --screenshot={out_path} --window-size={W},{H} http://127.0.0.1:{PORT}/
   ```

3. **Validate output**: reject any PNG smaller than 2 KB (likely a 1×1 placeholder).

4. **Register artifact** in `generated_files` so it is committed and pushed
   alongside the implementation code.

---

## Design Reference

Obtain the design reference screenshot from one of the following sources
(checked in priority order):

1. Screen-specific `imageUrls[0]` from a `stitch.screen.fetch` result
   (the JSON has a top-level `"screenId"` field).
2. Browser screenshot of `design_url` from `team-lead/design-context.json`.
3. Skip gracefully if neither source is available — never block implementation.

**Do NOT** use the project-level `thumbnailScreenshot.downloadUrl` from Stitch
project metadata; that thumbnail shows the project's default/first screen, which
may be a different page than the one being implemented.

---

## PR Description: Screenshots Section

The `## Screenshots` section of every PR must include:

1. The design reference alongside the implementation screenshots so reviewers
   can compare them without leaving GitHub.
2. All captured viewports embedded as Markdown images using GitHub raw URLs:

```markdown
## Screenshots

### Design Reference
![Design Reference](https://raw.githubusercontent.com/{owner}/{repo}/{branch}/docs/evidence/design-reference.png)

### Implementation

#### 1280×900
![Screenshot 1280x900](https://raw.githubusercontent.com/{owner}/{repo}/{branch}/docs/evidence/screenshot-1280x900.png)

#### 375×812
![Screenshot 375x812](https://raw.githubusercontent.com/{owner}/{repo}/{branch}/docs/evidence/screenshot-375x812.png)
```

3. If screenshots could not be captured (no Chromium, server did not start),
   note this explicitly — do not omit the section.

---

## Visual Review Checklist

Team Lead review should verify the following when screenshots are present:

### Layout
- [ ] No element overflow or unexpected horizontal scrollbar
- [ ] Content is visible and not clipped at any captured viewport
- [ ] The main page structure matches the Figma/Stitch design layout

### Responsive
- [ ] The 375px viewport renders a usable, readable layout
- [ ] Text is not truncated; buttons are tappable (min 44×44 px touch target)

### Design Fidelity
- [ ] Color palette, typography, and spacing are consistent with the design reference
- [ ] Navigation elements and key UI components are present

### Accessibility
- [ ] Sufficient contrast between text and background
- [ ] Interactive elements have visible focus states

---

## Best Practices

- **Capture first, commit together**: take screenshots in the same phase as
  code generation, before the git commit, so all evidence travels with the code.
- **Use real app startup, not static files**: screenshots taken from a running
  server catch runtime rendering issues that static HTML does not.
- **Skip gracefully**: if Chromium is unavailable or the server does not start,
  log a warning and continue — a missing screenshot is never a blocker for
  creating the PR.
- **Keep evidence small**: one screenshot per viewport is sufficient.
  Full-page screenshots or high-DPI captures are not needed unless the task
  explicitly requests them.
