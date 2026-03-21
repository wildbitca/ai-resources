---
name: flutter-feature-based-clean-architecture
description: "Standards for organizing Flutter code by feature for scalability. Use when structuring a Flutter project with feature-based clean architecture. (triggers: lib/features/**, feature, domain, infrastructure, application, presentation, modular)"
---

# Feature-Based Clean Architecture

## **Priority: P0 (CRITICAL)**

Standard for modular Clean Architecture organized by business features in `lib/features/`.

## SOLID Foundation

This architecture exists to enforce SOLID at the project level:

- **SRP**: Each feature module owns one bounded context; each layer within has a single responsibility.
- **OCP**: New features are new directories — existing features remain untouched.
- **LSP**: Repository implementations are interchangeable (prod, mock, cache) without changing consumers.
- **ISP**: Feature-level interfaces expose only what each consumer needs; avoid monolithic repository contracts.
- **DIP**: Domain defines abstractions (repository interfaces, use case contracts); Data layer provides implementations.

## Quality Attributes

| Attribute | How This Architecture Delivers It |
|-----------|----------------------------------|
| **Maintainability** | Changes scoped to one feature directory; no cross-feature side effects |
| **Reusability** | `lib/shared/` and `lib/core/` hold cross-cutting logic usable by all features |
| **Testability** | Domain is pure Dart with interfaces — mock any dependency in isolation |
| **Scalability** | Features are independent modules; teams can work in parallel without conflicts |
| **Extensibility** | New features = new directories; Strategy/Factory for behavioral variation within features |

## Structure

See [references/folder-structure.md](references/folder-structure.md) for the complete directory blueprint.

## Implementation Guidelines

- **Feature Encapsulation**: Keep logic, models, and UI internal to the feature directory.
- **Strict Layering**: Maintain 3-layer separation (Domain/Data/Presentation) within each feature.
- **Dependency Rule**: `Presentation -> Domain <- Data`. Domain must have zero external dependencies.
- **Cross-Feature Communication**: Features only depend on the **Domain** layer of other features.
- **Flat features**: Keep `lib/features/` flat; avoid nested features.
- **No DTO Leakage**: Never expose DTOs or Data Sources to UI or other features; return Domain Entities.
- **Shared logic**: Move cross-cutting concerns to `lib/shared/` or `lib/core/`.

## Reference & Examples

For feature folder blueprints and cross-layer dependency templates:
See [references/REFERENCE.md](references/REFERENCE.md).

## Design Patterns Applied

- **Repository**: Every feature defines a repository interface in `domain/` and its implementation in `data/`.
- **Use Case / Interactor**: Encapsulate single business operations; one public method per use case (SRP).
- **Strategy**: Encapsulate interchangeable algorithms (e.g., payment methods, sort algorithms) behind a shared interface within a feature.
- **Factory**: Use factories for complex entity creation; keep constructors clean.
- **Observer/Event Bus**: Decouple cross-feature communication via events — avoid direct imports between features.
- **Adapter**: Data sources adapt external APIs/SDKs into domain-friendly contracts.

## Related Topics

layer-based-clean-architecture | retrofit-networking | go-router-navigation | bloc-state-management | dependency-injection

> Cross-reference: See **common/best-practices** for SOLID enforcement and **common/system-design** for architectural patterns.

## 🚫 Anti-Patterns

- Do NOT use standard patterns if specific project rules exist.
- Do NOT ignore error handling or edge cases.
- Do NOT create fat repository interfaces — split by query/command or by entity if needed (ISP).
- Do NOT let features depend on another feature's `data/` or `presentation/` layer — only `domain/` (DIP).
