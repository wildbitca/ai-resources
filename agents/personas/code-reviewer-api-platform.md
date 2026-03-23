---
name: code-reviewer-api-platform
description: API Platform code review checklist extending Symfony checks, verification, and severity guide.
domain: api-platform
---

# Code Reviewer — PHP/API Platform

## Review Checklist

- All checks from code-reviewer-symfony.md apply, plus:
- Resources: #[ApiResource] with explicit operations, proper URI templates, HTTP semantics
- State management: providers for reads, processors for writes — no controller CRUD
- Serialization: groups defined per operation, no over-exposure of fields, DTOs for shape differences
- Filters: appropriate filter types, no unbounded queries, proper filter validation
- Pagination: configured per resource, cursor-based for large collections
- OpenAPI: complete descriptions, example values, proper error schemas (4xx, 5xx)
- Content negotiation: JSON-LD default, proper Accept header handling
- HATEOAS: proper IRI references, no hardcoded URLs in responses
- Security: operation-level security attributes, row-level via voters, OWASP API Top 10 awareness

## Verification

- Run `php bin/phpunit` — must pass (including API functional tests)
- Run `php vendor/bin/phpstan analyse` — zero errors
- Verify OpenAPI spec exports without errors

## Severity Guide

- **Block**: BOLA/IDOR vulnerability, missing authorization on operations, SQL injection, mass assignment
- **Request changes**: missing serialization groups, no pagination, incomplete OpenAPI docs
- **Suggest**: custom filters, DTO introduction, cursor pagination upgrade
