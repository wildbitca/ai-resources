---
name: supabase-flutter-integration
description: Supabase Postgres integration strategy: direct client vs backend proxy, RLS, JWT. Use when designing data access or backend integration for Flutter + Supabase.
triggers: "**/*.dart, supabase, PostgREST, RLS, row level security, direct client, backend proxy, supabase client, real-time subscription, data access layer"
---

# Supabase Postgres Integration Strategy

## Direct Client Access (PREFERRED)

- **PREFERRED:** Call Supabase PostgREST API directly for read operations.
- **BENEFIT:** Lower latency, better performance, real-time subscriptions.
- **SECURITY:** Row Level Security (RLS) provides database-level security.
- **USE CASES:** Feed queries, profile reads, like/comment operations, real-time subscriptions.

## Backend Proxy (Use when needed)

- **REQUIRED:** Use backend Workers or Edge Functions for: service keys, external API integration, complex business logic, rate limiting beyond RLS.
- **USE CASES:** Upload authorization, presigned URLs, complex aggregations, multi-source aggregation.

## Security with RLS

- **REQUIRED:** All Supabase tables must have RLS policies enabled.
- **REQUIRED:** RLS policies must validate authentication and authorization.
- **REQUIRED:** Use Supabase Flutter client with JWT; use transactions for critical operations.
- **BENEFIT:** Security enforced at database level, ACID compliance for critical ops.
