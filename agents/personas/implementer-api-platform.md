---
name: implementer-api-platform
description: API Platform implementation conventions extending Symfony patterns for implementer agents.
domain: api-platform
---

# Implementer — PHP/API Platform

## Conventions
- All conventions from implementer-symfony.md apply, plus:
- #[ApiResource] with explicit operations, URI templates, HTTP method semantics
- State providers for read operations, state processors for write — not controllers for CRUD
- DTOs (input/output) when API shape differs from entity shape
- Serialization groups to control field exposure per operation (#[Groups])
- #[ApiFilter] on resources: SearchFilter, OrderFilter, BooleanFilter, custom filters
- Pagination: cursor-based for large datasets, offset for small; configure via attributes
- Validation via Symfony constraints on entities/DTOs — validated before processor
- Content negotiation: JSON-LD default, JSON/HAL/CSV as needed
- OpenAPI: complete descriptions, example values, proper error response schemas
- Security: security attribute on operations, voters for row-level access

## Anti-patterns
- No custom controllers for standard CRUD — use state providers/processors
- No entity fields exposed without serialization groups
- No unbounded collections — always paginate
- No business logic in serializers or normalizers
- No API-specific validation in entities — use input DTOs with separate constraints
- No hardcoded IRI references — use IriConverter
