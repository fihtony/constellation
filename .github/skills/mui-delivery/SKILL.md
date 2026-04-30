---
name: mui-delivery
description: >
  Material UI (MUI v5/v6) delivery playbook. Use when building React UIs with MUI components,
  sx prop, theme customisation, Grid, Box, or Typography.
user-invocable: false
---

# Material UI (MUI) Delivery

## When To Apply

- React UIs using `@mui/material` (MUI v5 or v6).
- Layouts with `Grid`, `Stack`, `Box`, responsive design.
- Custom themes, color palettes, and typography scales.

## Component Usage Rules

- Import from `@mui/material`: `import { Button, TextField, Grid } from '@mui/material'`.
- Use the `sx` prop for one-off styles; define shared styles in the theme or a `styled()` component.
- Prefer `Stack` for simple one-direction layouts; use `Grid` (v2: `Grid2`) for two-dimensional grids.
- `TextField` handles label, helper text, and error state in a single component — use it instead of raw `<input>`.
- `Button` variants: `contained` (primary action), `outlined` (secondary), `text` (tertiary/link-like).
- Dialog: controlled by `open` boolean; include `onClose` to allow dismissal via backdrop/Esc.
- Use `Snackbar` + `Alert` for transient user feedback; use `Dialog` for confirmations.

## Theme Customisation

- Create theme with `createTheme({ palette: { primary: { main: '#...' } }, ... })`.
- Wrap the app in `ThemeProvider` — never apply MUI-specific styles outside it.
- Use `theme.palette`, `theme.spacing()`, `theme.breakpoints.up()` in `sx` / `styled()` — no magic numbers.
- Dark mode: toggle via `palette.mode: 'dark'` in the theme; use `useColorScheme()` hook (MUI v6).

## Responsive Design

- Mobile-first: start with smallest breakpoint, override upward.
- `sx={{ display: { xs: 'none', md: 'block' } }}` for responsive visibility.
- `Grid` columns with `xs`, `sm`, `md` props for fluid column counts.

## Quality Checklist

- No raw CSS pixel values where `theme.spacing()` or `theme.breakpoints` applies.
- All interactive elements have accessible labels (`aria-label` or visible text).
- Form fields use `error` and `helperText` props — not separate `<span>` error messages.
- No MUI peer-dependency version mismatches between `@mui/material` and `@emotion/react`.
- Consistent use of theme tokens; no one-off inline colour overrides.
