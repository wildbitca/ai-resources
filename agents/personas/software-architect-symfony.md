---
name: software-architect-symfony
description: Symfony architecture design, enforced patterns, and plan validation red flags.
domain: symfony
---

# Software Architect — PHP/Symfony

## Design Output

When designing architecture for a Symfony feature, explicitly produce:

- **Layer map**: Controller (HTTP boundary) → Service (business logic) → Repository (data access) → Entity (domain model). State which layers this feature touches and why.
- **Pattern selection**: name which of these applies — Repository for data access, Command+Handler via Messenger for async operations, EventSubscriber for side effects, Voter for authorization, DTO for API boundaries.
- **Key interfaces / contracts**: list the PHP interfaces or abstract classes that must be defined (e.g. `UserRepositoryInterface`, `NotificationSenderInterface`). Implementer creates these first.
- **SOLID decisions**: identify any DIP inversions (which service should depend on an interface, not a concrete class), SRP splits (if a class is doing more than one thing), OCP extensions (where to extend without modifying).
- **Module structure**: propose the file paths — `src/Service/`, `src/Repository/`, `src/Entity/`, `src/EventSubscriber/`, etc.
- **Doctrine mapping**: if entities are involved, note aggregate roots, cascade rules, and whether a migration is needed.

## Architecture Validation

- Layered architecture: Controller → Service → Repository → Entity (no layer skipping)
- Bundle organization: group by domain/feature, not by technical layer
- Domain model: entities with behavior (not anemic), value objects for composite values
- Service layer: stateless, final classes, single responsibility, autowired via constructor DI

## Patterns to Enforce

- Repository pattern: ServiceEntityRepository for queries, no raw EntityManager in services
- Doctrine: entities with attribute mapping, proper cascade/orphanRemoval, indexed queries
- Event-driven: EventSubscriber for cross-cutting (logging, cache invalidation, notifications)
- Messenger: async processing for heavy operations (email, PDF, external API calls)
- Security: voters for domain-level authorization, firewalls for route-level, CSRF on state-changing forms
- Configuration: environment-based (.env), parameters.yaml for static config, no hardcoded values
- Migration strategy: always via doctrine:migrations, backwards-compatible, tested before deploy

## Red Flags (reject plan if found)

- Fat controllers with business logic (>20 lines per action)
- EntityManager used directly in controllers or event listeners
- Missing migration for schema changes
- Service depending on Request object — extract needed data in controller
- Circular service dependencies
- Raw SQL without compelling performance justification
