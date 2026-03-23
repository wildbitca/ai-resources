---
name: implementer-symfony
description: PHP/Symfony implementation conventions and anti-patterns for implementer agents.
domain: symfony
---

# Implementer — PHP/Symfony

## Conventions

- PHP 8.2+: readonly classes, enums, named arguments, match expressions, fibers, union/intersection types
- Thin controllers: max 1 service call + response; delegate logic to services
- Autowired services via constructor injection, final classes by default
- Doctrine entities with PHP attributes (#[ORM\Entity]), repositories extending ServiceEntityRepository
- Migrations via doctrine:migrations:diff — never write DDL manually
- Symfony Validator constraints as attributes on entities/DTOs
- Security: #[IsGranted], voters for complex authorization, CSRF on forms
- Event system: EventSubscriberInterface for cross-cutting concerns
- Messenger for async: proper transport config, retry strategy, failure transport
- Configuration via parameters.yaml/.env — no hardcoded credentials or URLs
- Proper exception hierarchy: domain exceptions → HTTP exceptions in controller layer

## Anti-patterns

- No raw SQL without parameterized queries — use Doctrine DQL/QueryBuilder
- No business logic in controllers or entities — services only
- No service locator pattern (Container::get) — use DI
- No suppressed exceptions (@) or empty catch blocks
- No mixed return types when specific types are possible
- No direct $_GET/$_POST/$_SESSION — use Symfony Request/Session
