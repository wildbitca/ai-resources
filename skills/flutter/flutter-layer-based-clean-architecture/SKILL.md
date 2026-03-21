---
name: flutter-layer-based-clean-architecture
description: "Standards for separation of concerns, layer dependency rules, and DDD in Flutter. Use when applying DDD or layer-based clean architecture in Flutter. (triggers: lib/domain/**, lib/infrastructure/**, lib/application/**, domain, infrastructure, application, presentation, layers, dto, mapper)"
---

# Layer-Based Clean Architecture

## **Priority: P0 (CRITICAL)**

Standardized separation of concerns and dependency flow using DDD principles.

## SOLID Foundation

Each layer enforces specific SOLID principles:

- **SRP**: Domain entities model one concept; repositories handle one aggregate; BLoCs orchestrate one workflow.
- **OCP**: New behavior via new use cases or BLoC events — existing domain logic stays closed for modification.
- **LSP**: All repository implementations (remote, local, mock) honor the interface contract identically.
- **ISP**: Split large repository interfaces by aggregate or operation type; consumers depend only on what they use.
- **DIP**: Domain defines abstractions (repository interfaces, failure types); Infrastructure provides implementations. Domain has zero external dependencies.

## Quality Attributes

| Attribute | How This Architecture Delivers It |
|-----------|----------------------------------|
| **Maintainability** | Clear layer boundaries; changes in infrastructure don't ripple into domain |
| **Reusability** | Domain entities and interfaces reusable across multiple UI surfaces or services |
| **Testability** | Pure Dart domain + interface-based repositories = trivial to mock and unit-test |
| **Scalability** | Layers can be scaled independently; stateless BLoCs enable parallel development |
| **Extensibility** | New behavior via new use cases, new BLoC events — existing domain stays closed (OCP) |
| **Portability** | Swap infrastructure (Dio→GraphQL, Hive→SQLite) without touching domain or application |

## Structure

```text
lib/
├── domain/ # Pure Dart: entities (@freezed), failures, repository interfaces
├── infrastructure/ # Implementation: DTOs, data sources, mappers, repo impls
├── application/ # Orchestration: BLoCs / Cubits
└── presentation/ # UI: Screens, reusable components
```

## Implementation Guidelines

- **Dependency Flow**: `Presentation -> Application -> Domain <- Infrastructure`. Dependencies point inward.
- **Pure Domain**: No Flutter (Material/Store) or Infrastructure (Dio/Hive) dependencies in `Domain`.
- **Functional Error Handling**: Repositories must return `Either<Failure, Success>`.
- **Always Map**: Infrastructure must map DTOs to Domain Entities; do not leak DTOs to UI.
- **Immutability**: Use `@freezed` for all entities and failures.
- **Logic Placement**: No business logic in UI; widgets only display state and emit events.
- **Inversion of Control**: Use `get_it` to inject repository implementations into BLoCs.

## Anti-Patterns

- **No DTOs in UI**: Never import a `.g.dart` or Data class directly in a Widget.
- **No Material in Domain**: Do not import `package:flutter/material.dart` in the `domain` layer.
- **No Shared Prefs in Repo**: Do not use `shared_preferences` directly in a Repository; use a Data Source.

## Reference & Examples

For full implementation templates and DTO-to-Domain mapping examples:
See [references/REFERENCE.md](references/REFERENCE.md).

## Design Patterns Applied

- **Repository**: Abstract data access behind domain interfaces; implementations live in Infrastructure.
- **Data Mapper**: DTOs map to domain entities at the infrastructure boundary — domain never sees raw data shapes.
- **Use Case / Interactor**: Single-purpose orchestrators in the Application layer (one public `call()` method).
- **Strategy**: Encapsulate behavioral variation (e.g., auth providers, export formats) behind interfaces in Domain.
- **Factory**: Centralize complex entity/DTO creation; hide construction details from consumers.
- **Observer/Event Bus**: Decouple cross-module communication; Application layer emits events, other layers subscribe.
- **Adapter**: Data sources adapt external SDKs (Dio, Hive, Supabase) into infrastructure-layer contracts.

## Related Topics

feature-based-clean-architecture | bloc-state-management | dependency-injection | error-handling

> Cross-reference: See **common/best-practices** for SOLID enforcement and **common/system-design** for architectural patterns.
