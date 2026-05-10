# React & Next.js Development

## Framework Rules

- Use React 18+ with functional components and hooks only
- Use Next.js 14+ App Router (not Pages Router)
- All components must be TypeScript (.tsx)
- Use Server Components by default; add `"use client"` only when needed
- Prefer `next/image` for images, `next/link` for navigation

## File Organization

- Components in `src/components/`
- Pages in `src/app/`
- Utilities in `src/lib/`
- Types in `src/types/`

## State Management

- Use React Context + useReducer for global state
- Use SWR or React Query for server state
- Avoid prop drilling beyond 2 levels

## Styling

- Use CSS Modules or Tailwind CSS
- Follow the existing project convention

## Testing

- Write tests with Jest + React Testing Library
- Test user interactions, not implementation details
- Minimum: one test per component, one test per hook

## Error Handling

- Use Error Boundaries for component errors
- Use `error.tsx` for route-level errors in App Router
- Always show user-friendly error messages
