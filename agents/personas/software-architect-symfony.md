---
name: software-architect-symfony
description: Symfony architecture validation, enforced patterns, and plan rejection red flags.
domain: symfony
---

# Software Architect — PHP/Symfony

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
