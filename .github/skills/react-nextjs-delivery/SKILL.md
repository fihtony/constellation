---
name: react-nextjs-delivery
description: >
  React and Next.js delivery playbook. Use when implementing React components, hooks,
  Next.js pages/routing, SSR/SSG/ISR patterns, or API routes.
user-invocable: false
---

# React / Next.js Delivery

## When To Apply

- Building React components, custom hooks, or context providers.
- Next.js app: file-based routing, page components, layout hierarchy, middleware.
- Server-side rendering (SSR), static generation (SSG), incremental static regeneration (ISR).
- Next.js API routes (`/pages/api/` or `/app/api/` route handlers).

## Component Rules

- Prefer functional components with hooks; avoid class components.
- Keep components small and single-purpose. Extract sub-components when JSX exceeds ~80 lines.
- Lift state only as far as it needs to go. Use `Context` for cross-tree globals, not for everything.
- Memoize with `useMemo`/`useCallback` only when measurable re-render cost exists.
- Avoid `any` in TypeScript; define explicit prop types or interfaces.

## Next.js Patterns

- App Router (`/app`) preferred over Pages Router (`/pages`) for new projects.
- Use `generateStaticParams` for dynamic static routes; use `revalidate` for ISR.
- Co-locate route-specific components, loading.tsx, and error.tsx files next to the page.
- Server Components are the default; opt into `'use client'` only for interactivity or browser APIs.
- Use `next/image` for images, `next/link` for navigation — never raw `<img>` or `<a>` for internal links.
- Environment variables: server-side in `.env.local`, client-side with `NEXT_PUBLIC_` prefix.

## Data Fetching

- In Server Components: fetch directly with `async/await` (no useEffect needed).
- In Client Components: use `SWR` or `React Query` for remote data with caching and revalidation.
- Never expose secret keys in Client Components or API responses.

## Quality Checklist

- All pages render without JavaScript (SSR/SSG) unless they are explicitly client-only.
- Loading and error states are handled for every async operation.
- Forms use controlled inputs with validation feedback; never submit without client-side check.
- Routes are tested: both happy path and 404/error fallback.
- No `console.error` or hydration warnings in the browser.
