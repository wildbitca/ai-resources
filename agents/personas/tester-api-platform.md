---
name: tester-api-platform
description: API Platform test strategy extending Symfony tests with API-specific coverage.
domain: api-platform
---

# Tester — PHP/API Platform

## Test Strategy

- All conventions from tester-symfony.md apply, plus:
- API functional tests: ApiTestCase for each resource operation (GET collection, GET item, POST, PUT, PATCH, DELETE)
- Test HTTP semantics: status codes (200, 201, 204, 400, 403, 404, 422), content types, HATEOAS links
- Filter tests: verify SearchFilter, OrderFilter, custom filters return correct subsets
- Serialization tests: verify response shape matches serialization groups per operation
- OpenAPI validation: exported spec matches implementation

## Commands (ALL must exit 0)

- `php bin/phpunit` — full test suite including API functional tests
- `php vendor/bin/phpstan analyse` — static analysis
- `php vendor/bin/php-cs-fixer fix --dry-run --diff` — coding standards
- `php bin/console api:openapi:export` — OpenAPI spec export (verify no errors)

## Conventions

- API tests use ApiTestCase: createClient(), request('GET', '/api/resources'), assertJsonContains()
- Test pagination: verify page size, next/previous links, total count
- Test authorization per operation: owner access, other-user denied, admin override
- Test validation: POST/PUT with invalid data → 422 with violation details
- Fixtures via Foundry for API test data — deterministic, isolated

## Anti-patterns

- No testing only happy paths — test 400, 403, 404, 422 responses
- No assertions on database state when API response is sufficient
- No hardcoded IRI paths — use IriConverter or findIriBy()
