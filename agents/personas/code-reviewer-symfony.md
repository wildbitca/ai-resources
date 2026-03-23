---
name: code-reviewer-symfony
description: PHP/Symfony code review checklist, verification commands, and severity guide.
domain: symfony
---

# Code Reviewer — PHP/Symfony

## Review Checklist

- PHP 8.2+: readonly, enums, named args, match, typed properties, union types used correctly
- Architecture: thin controllers (1 service call + response), services are final, proper DI
- Doctrine: no N+1 queries, proper eager/lazy loading, indexed columns for filtered queries
- Migrations: backwards-compatible changes, no data loss, generated via doctrine:migrations:diff
- Security: voters for authorization, CSRF on forms, parameterized queries only
- Validation: Symfony constraints on entities/DTOs, custom validators where needed
- Error handling: proper exception hierarchy, domain → HTTP mapping in controller layer
- Configuration: no hardcoded values, proper use of .env/parameters.yaml
- Events: EventSubscriber for cross-cutting, proper event propagation
- Messenger: async handlers with retry/failure strategy, idempotent message handling

## Verification

- Run `php bin/phpunit` — must pass
- Run `php vendor/bin/phpstan analyse` — zero errors at configured level
- Run `php vendor/bin/php-cs-fixer fix --dry-run` — zero violations

## Severity Guide

- **Block**: SQL injection risk, missing auth checks, raw SQL without parameters, secrets in code
- **Request changes**: N+1 queries, missing validation, manual DDL without migration
- **Suggest**: readonly properties, enum usage, named arguments for clarity
