# Design-to-Code Workflow

Generic guidance for any development agent implementing UI from design sources
(Figma, Google Stitch, or similar design tools).

## Source-of-Truth Rules

- Treat the task prompt as the operating contract: project directory, output
  locations, validation steps, screenshots, and any task-specific custom
  requirements are all hard requirements for the current task.
- When multiple design inputs are provided, use this priority order:
  1. Explicit task instructions
  2. Reference HTML / exact markup exported from the design tool
  3. Design spec tokens and component guidance
  4. Reference screenshots
- Do not invent extra sections, alternate routes, placeholder content, or
  theme variants that are absent from the supplied design source.
- Only keep optional theme variants such as `dark:` classes when they are
  explicitly present in the supplied reference source or explicitly required
  by the task.

## Project Safety Rules

- All files must be written inside the project directory provided by the task.
- Never write files to a parent directory or sibling workspace.
- Use relative paths for file operations.
- If the scaffold already exists, preserve it.  Never overwrite `package.json`
  from scratch.
- If you need dependencies, install them with the package manager instead of
  rewriting config files blindly.

## Workflow (follow this order)

1. **PLAN** — Create a short todo list based on the actual page structure from the
   design source.
2. **READ** — Inspect existing project files before editing.  Confirm whether the
   scaffold, build script, and config already exist.
3. **SCAFFOLD OR REPAIR** — If the scaffold is missing, create it in-place.  If
   it exists but is broken, repair the minimum needed.
4. **CONFIGURE** — Install only the required dependencies and write the minimum
   config needed for the requested stack.
5. **IMPLEMENT** — Translate the supplied design into components that match the
   actual sections and hierarchy in the design source.  Use component names
   and file names that match the current page, not a hardcoded template from
   a previous task.
6. **LOCAL BRANCH** — For repo-backed work, create or reuse the local development
  branch inside the cloned repository before writing files.
7. **BUILD** — Run the project build command and required tests locally and verify
  they succeed, or explicitly record the blocking failure.
8. **VERIFY OUTPUT** — Inspect the build output immediately after each
   successful build.
9. **AUDIT DESIGN** — Compare each component/section one by one against the
   supplied design source.
10. **CAPTURE EVIDENCE** — Save the design reference screenshot and the
   implemented UI screenshots in the task workspace. For repo-backed work,
   commit PR-safe copies under `docs/evidence/` and reference them in the PR
   description.
11. **FIX LOOP** — If any item is missing, redundant, wrong, or unverified,
   fix it and rebuild.
12. **FINALIZE** — Produce only the artifacts requested by the task, at the
    requested locations.

## Colour Discipline

- Every UI section's background colour MUST be derived from the design's
  colour token palette (surface, surface-container, primary, inverse-surface,
  etc.).
- Never apply black (#000000), CSS default, or a transparent background to a
  section unless the design explicitly specifies that exact colour.
- Dark sections (hero banners, headers, footers, cards) typically use the
  design's `primary`, `inverse-surface`, or `surface-container-highest`
  colour — never assume black.
- After implementing each section, cross-check its background hex value
  against the design tokens before moving on.

## Design Audit Rules

- After every successful build, compare the implementation against the design
  source one component/section at a time.
- Check exact text, semantic tags, href/button/icon/data attributes, class
  tokens, colors, spacing, typography, border radius, shadows, responsive
  layout, and child order.
- Record findings using these buckets:
  - IMPLEMENTED
  - MISSING
  - REDUNDANT
  - WRONG
  - NEXT FIX
- Do not report DONE while any MISSING, REDUNDANT, or WRONG item remains.

## Completion Rules

- Do not stop after writing source files.  You are not done until build output
  exists and required artifacts are verified.
- If the task requires screenshots, generate them exactly where the task says.
- For UI tasks with design context, capture both the original design reference and the implemented
  result, and ensure the PR description points to both.
- If the task requires a README, write it.
- Output completion only after the requested page matches the supplied design
  closely, the output is compiled correctly, and requested task-specific
  artifacts are present.
