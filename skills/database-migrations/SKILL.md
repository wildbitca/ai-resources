---
name: database-migrations
description: Enforces schema migration practices for Supabase (or similar) with file-based migrations, naming, idempotency, and a standard workflow. Use when creating or modifying database schema, or when working with Supabase migrations.
---

# Database Migrations

## Where and How

- All schema changes go through the migration system (e.g. Supabase CLI); files live in `supabase/migrations/` (or project equivalent).
- **PROHIBITED:** Applying schema changes only via Dashboard SQL or by editing already-applied migrations.

## File Naming

- Pattern: `<YYYYMMDDHHMMSS>_<descriptive_name>.sql`.
- **PROHIBITED:** Using future dates. Use the current date only when it sorts after the latest existing migration.
- **Incremental mode (active):** The latest migration is dated `20260706` (July 6, 2026). Until the real calendar date reaches or exceeds that, new migrations MUST reuse the latest date prefix and increment the sequence portion: `20260706120008_...`, `20260706120009_...`, etc.
- Once the real date surpasses the latest migration date, resume using actual dates with `HHMMSS=120000` as the base sequence.
- Example (incremental): `20260706120008_add_index_on_posts_pet_id.sql`.
- Example (date-based, after catch-up): `20260810120000_add_reviews_table.sql`.

## Idempotency

- Prefer `IF NOT EXISTS` / `IF EXISTS` so re-running is safe.
- Test with `supabase db reset` (or project’s reset command) before applying to shared envs.
- When dropping, use `CASCADE` where appropriate and document any non-trivial or hard-to-rollback changes.

## Workflow

1. Create a new migration file with the naming pattern.
2. Write idempotent SQL.
3. Test: run reset and re-apply.
4. Apply via `supabase db push` (or project’s command).
5. Confirm schema and, if needed, RLS/triggers.

## Rollback

- For complex or risky migrations, document the rollback steps in the migration file or in a separate doc.
