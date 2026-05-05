# React + Tailwind CSS Workflow

Web-agent-specific guidance for implementing React + Tailwind CSS projects from
design references.

## React + Tailwind v3 Rules

- If the task asks for React + Tailwind, use **Tailwind CSS v3**, not v4.
- Install Tailwind with
  `npm install -D tailwindcss@3 postcss autoprefixer` and generate config
  with `npx tailwindcss init -p`.
- Ensure `postcss.config.*` includes `tailwindcss` and `autoprefixer`.
- Ensure `vite.config.*` includes `@vitejs/plugin-react` when the project
  uses Vite + React.
- Use Google Fonts via CSS `@import` when the design uses hosted web fonts.
- Keep styles in Tailwind utilities and design tokens.  Avoid inline styles
  unless the task explicitly requires them.
- Configure design tokens in `tailwind.config.js`: colors, fonts, spacing,
  borderRadius, and any other tokens the supplied design actually uses.

## CSS Bundle Discipline

- After each build, run `wc -c dist/assets/*.css` and
  `grep "@tailwind" dist/assets/*.css`.
- A small single-screen page should usually compile to the low tens of KB,
  not a massive utility dump.
- If CSS is tiny or still contains raw `@tailwind` directives, Tailwind /
  PostCSS is misconfigured.
- If CSS is very large, remove the cause instead of padding around it.
- Never use `safelist`, `pattern: /.*/`, large `raw:` content blocks, dummy
  markup, or filler comments to inflate output.

## Tailwind Config Forbidden Patterns

The following patterns MUST NOT appear in `tailwind.config.js`:
- `safelist:`
- `pattern: /.*/`
- `raw:`
