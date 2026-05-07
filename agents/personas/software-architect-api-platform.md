---
name: software-architect-api-platform
description: API Platform architecture design, resource modeling, pattern selection, and plan validation.
domain: api-platform
---

# Software Architect — PHP/API Platform

## Design Output

When designing architecture for an API Platform feature, explicitly produce (in addition to Symfony's Design Output):

- **Resource design**: name the API resource(s), their operations (GET collection, GET item, POST, PATCH, DELETE — only what's needed), and HTTP semantics. Justify any non-standard operations.
- **Read/write path separation**: identify which operations need State Providers (read) vs State Processors (write). Name the classes.
- **DTO decision**: does the API shape match the entity? If not, define the Input DTO and/or Output DTO class names and their fields.
- **Serialization groups**: propose the group names per operation (e.g. `user:read`, `user:write`, `user:list`). State which fields belong to which group.
- **Filter design**: list the filters to expose per resource (Search, Order, Boolean, custom) and their parameter names.
- **Authorization design**: which Voter handles authorization for each operation? Define the attribute constants (e.g. `UserVoter::EDIT`).
- **OpenAPI contract**: note any required schema descriptions, examples, or error responses the implementer must document.

## Architecture Validation

- All principles from software-architect-symfony.md apply, plus:
- Resource-centric design: each API resource maps to a domain concept (not a DB table)
- Operations: explicit, minimal set per resource (no unused CRUD endpoints)
- State providers/processors: separation of read and write paths
- DTOs: input/output DTOs when API shape ≠ entity shape (common for create/update)

## Patterns to Enforce

- #[ApiResource] with explicit operations, named routes, proper HTTP semantics
- Serialization groups: per-operation field exposure, nested object control
- Filters: declared on resource, proper type (Search, Order, Boolean, Range, custom)
- Pagination: strategy per resource (cursor for feeds, offset for admin lists), reasonable page size
- Subresource design: prefer flat endpoints with filters over deep nesting (/posts?author=1 vs /authors/1/posts)
- Validation: Symfony constraints on input DTOs, custom validators for business rules
- OpenAPI: complete schema descriptions, examples, error response documentation
- Versioning: via content negotiation or URL prefix, backwards-compatible changes preferred

## Red Flags (reject plan if found)

- Custom controller for standard CRUD — must justify why providers/processors won't work
- Entity exposed directly without serialization groups (leaks internal fields)
- Unbounded collection endpoint without pagination
- Missing authorization on write operations
- N+1 in state provider — use Doctrine query optimization
- Breaking API change without versioning strategy
