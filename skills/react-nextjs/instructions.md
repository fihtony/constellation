# React & Next.js Development

## Framework Rules

- Use React 18+ with functional components and hooks only
- Next.js projects: use Next.js 14+ App Router (not Pages Router)
- Vite+React projects: use JSX files (.jsx / .tsx) with Vite config
- Use Server Components by default in Next.js; add `"use client"` only when needed
- Prefer `next/image` for images, `next/link` for navigation

## File Organization

- Components in `src/components/`
- Pages in `src/app/` (Next.js) or `src/pages/` (Vite)
- Utilities in `src/lib/`
- Styles in `src/styles/` (CSS files) or co-located `.module.css`

## State Management

- Use React Context + useReducer for global state
- Use SWR or React Query for server state
- Avoid prop drilling beyond 2 levels

## Styling

- Extract all design tokens (colors, fonts, spacing) from the design spec/HTML
- Use CSS custom properties (variables) for design tokens
- Use CSS Modules or plain CSS — follow the existing project convention
- Match the design spec exactly: color values, font families, layout grid

## Package Management (MANDATORY)

Before adding any package to package.json:
1. Verify the package exists: `npm info <package-name> version`
   - If the command errors or returns nothing, the package does NOT exist — skip it
2. After writing package.json, always run `npm install` to verify all packages resolve
3. Always run `npm run build` after install to catch compilation errors early

When using Tailwind CSS:
- Add `tailwindcss` and `autoprefixer` to devDependencies in package.json
- Create postcss.config.js referencing both plugins
- Create tailwind.config.js with the correct content paths
- Always run `npm install` after adding these packages
- IMPORTANT: .gitignore must include `node_modules/`, `.vite/`, `dist/` before committing

Known valid testing packages for Vite+React:
- `vitest` — test runner (NOT `vitest-environment-jsdom` — that package does not exist)
- `@vitest/ui` — Vitest UI
- `@testing-library/react` — React Testing Library
- `@testing-library/jest-dom` — custom matchers
- `jsdom` — DOM environment for Vitest (configure via `test.environment: 'jsdom'` in vite.config.js)

## Testing

- Vite+React: use vitest + jsdom + @testing-library/react
- Next.js: use jest + jest-environment-jsdom + @testing-library/react
- Configure test environment in vite.config.js (for Vitest): `test: { environment: 'jsdom' }`
- Write tests for user interactions, not implementation details
- Minimum: one test per component, one test per page

## Error Handling

- Use Error Boundaries for component errors
- Use `error.tsx` / `error.jsx` for route-level errors in App Router
- Always show user-friendly error messages
