---
name: cloudflare-workers-auth
description: JWT validation and presigned URL security for Cloudflare Workers with Supabase. Use when implementing protected endpoints or upload flows in Workers.
---

# Cloudflare Workers Security

## JWT Validation

- **REQUIRED:** All requests to protected backend endpoints must include JWT in `Authorization` header.
- **REQUIRED:** Worker must verify token authenticity before processing business logic.
- **REQUIRED:** Use `jose` library for local signature verification using asymmetric keys (JWKS from Supabase).
- **PROHIBITED:** Calling Supabase API for each token validation (causes unnecessary latency).
- **BENEFIT:** Fast, secure edge validation without extra API calls.

## Presigned URLs

- **REQUIRED:** Presigned URLs must have 5–10 minute expiration (`PRESIGNED_URL_EXPIRY_SECONDS = 600`).
- **REQUIRED:** Validate JWT before generating presigned URLs; only authorized users can upload to their folders.

## Security

- **REQUIRED:** Edge validation—validate all write operations in Worker before database.
- **REQUIRED:** RLS policies on Supabase for additional defense in depth.
