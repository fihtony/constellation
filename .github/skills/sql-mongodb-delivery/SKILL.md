---
name: sql-mongodb-delivery
description: >
  SQL and MongoDB delivery playbook. Use when designing schemas, writing migrations,
  building queries, or working with ORMs (SQLAlchemy, Prisma, Mongoose, etc.).
user-invocable: false
---

# SQL / MongoDB Delivery

## When To Apply

- Designing relational schemas (PostgreSQL, MySQL, SQLite).
- Writing database migrations with Alembic (Python) or Flyway/Liquibase (JVM).
- Building queries: raw SQL, SQLAlchemy ORM, Prisma, or TypeORM.
- MongoDB schema design, Mongoose models, aggregation pipelines.

## SQL Schema Rules

- Every table has a surrogate primary key (`id SERIAL PRIMARY KEY` or `id BIGINT GENERATED ALWAYS AS IDENTITY`).
- Use `NOT NULL` constraints by default; allow `NULL` only when absence is meaningful.
- Foreign keys must have indexes on the referencing column.
- Use `TIMESTAMPTZ` (Postgres) or `DATETIME` for timestamps; always store UTC.
- Avoid `TEXT` for enum-like columns; use `CHECK` constraints or a dedicated lookup table.
- Table and column names: `snake_case`, plural table names (`users`, `orders`).

## Migrations

- **Alembic** (Python/SQLAlchemy): `alembic revision --autogenerate -m "description"` then review before applying.
  - Never hand-edit `alembic_version` or skip versions.
  - Each migration must be reversible (`upgrade` + `downgrade`).
- **Flyway** (JVM): naming `V{version}__{description}.sql`; never modify applied scripts.
- **Prisma**: `prisma migrate dev` for development; `prisma migrate deploy` for production.
- Run migrations as part of app startup in CI/CD, not as a manual step.

## Query Rules

- Avoid `SELECT *`; list columns explicitly.
- Use parameterised queries / ORM bindings — never string-interpolate user input into SQL.
- Add pagination (`LIMIT`/`OFFSET` or keyset pagination) to all list endpoints.
- Use `EXPLAIN ANALYZE` to verify query plans for queries on large tables.

## MongoDB Schema Rules

- Define schemas with Mongoose (`new Schema({ ... }, { timestamps: true })`).
- Use `required: true` and `type` for all fields — treat Mongoose schemas as the source of truth.
- Index fields used in queries: `schema.index({ field: 1 })`.
- Avoid storing large arrays that grow unboundedly in a single document.
- Use references (`ObjectId` + `ref`) for large or frequently updated related data; embed for small, stable sub-documents.

## Quality Checklist

- Migrations run without errors on a clean database.
- No N+1 query patterns: use `JOIN`, `include`, or `populate` rather than fetching in a loop.
- Sensitive data (passwords, tokens) is never stored in plain text.
- Indexes exist for all columns used in `WHERE`, `ORDER BY`, and `JOIN` conditions.
- Connection pooling is configured; connection strings come from environment variables.
