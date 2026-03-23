---
name: tester-symfony
description: PHP/Symfony test strategy, required commands, and testing conventions.
domain: symfony
---

# Tester — PHP/Symfony

## Test Strategy

- Unit tests: services, validators, event subscribers, DTOs, value objects (PHPUnit)
- Functional tests: controllers via WebTestCase, check HTTP status, response content, redirects
- Integration tests: repositories via KernelTestCase with test database
- Migration tests: verify doctrine:migrations:migrate runs cleanly

## Commands (ALL must exit 0)

- `php bin/phpunit` — full test suite
- `php vendor/bin/phpstan analyse` — static analysis at project-configured level
- `php vendor/bin/php-cs-fixer fix --dry-run --diff` — coding standards check

## Conventions

- Test file mirrors source: `src/Service/FooService.php` → `tests/Service/FooServiceTest.php`
- Functional tests extend WebTestCase: createClient(), request(), assertResponseStatusCodeSame()
- Use test database (SQLite or dedicated test DB via .env.test)
- Fixtures via Foundry or DoctrineFixturesBundle for repeatable test data
- Mock external services (HTTP clients, mailers, message bus) — never hit real APIs
- Test validation: assert constraint violations on invalid data
- Test security: verify voters, assert 403 for unauthorized access

## Anti-patterns

- No tests that depend on database state from other tests — reset between tests
- No testing private methods directly — test through public interface
- No raw SQL assertions — use Doctrine repository methods
