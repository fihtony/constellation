---
name: nodejs-express-delivery
description: >
  Node.js and Express delivery playbook. Use when building REST APIs, middleware,
  async route handlers, or Express-based backend services.
user-invocable: false
---

# Node.js / Express Delivery

## When To Apply

- Building REST API servers with Express (v4 or v5).
- Adding middleware: authentication, logging, rate limiting, CORS, body parsing.
- Async route handlers with `async/await`.
- Environment-based configuration with `dotenv`.

## Project Structure

```
src/
  app.js          # Express app factory (exported, not listening)
  server.js       # Entry point — calls app.listen()
  routes/         # Router files, one per resource
  middleware/     # Custom middleware
  controllers/    # Route handler logic
  services/       # Business logic
  models/         # Data models / ORM schemas
```

- Separate the app factory from `server.js` to allow testing without starting a port.
- Group routes by resource: `routes/users.js`, `routes/products.js`, etc.

## Route Handler Rules

- Always use `async/await` with `try/catch` or a wrapper (`asyncHandler`) — never leave unhandled promise rejections.
- Return consistent JSON: `{ data: ... }` for success, `{ error: { message, code } }` for errors.
- Validate request body/params with `express-validator` or `zod` before processing.
- Use `res.status(code).json(payload)` — never `res.send()` for JSON APIs.
- Do not hardcode status codes; use named constants or `http-status-codes` package.

## Middleware

- Register error-handling middleware last with 4 params: `(err, req, res, next)`.
- Use `helmet()` for security headers and `cors()` for cross-origin control.
- Use `express.json()` and `express.urlencoded({ extended: true })` for body parsing.

## Environment Configuration

- Load with `dotenv` at the top of `server.js`: `require('dotenv').config()`.
- Never commit `.env` files; provide `.env.example` with placeholder values.
- Access via `process.env.VAR_NAME`; validate required vars at startup.

## Quality Checklist

- All async handlers have error handling; unhandled errors reach the error middleware.
- Input validation runs before controller logic; invalid requests return 400 with a clear message.
- No secrets in source code or logs.
- Tests use `supertest` against the app factory; no live network calls in unit tests.
- `npm start` runs the server; `npm test` runs the test suite — both must work.
