---
name: common-best-practices
description: "Universal clean-code principles for any environment. (triggers: **/*.ts, **/*.tsx, **/*.go, **/*.dart, **/*.java, **/*.kt, **/*.swift, **/*.py, solid, kiss, dry, yagni, naming, conventions, refactor, clean code)"
---

# Global Best Practices

## **Priority: P0 (FOUNDATIONAL)**

## 🏗 Core Principles

### SOLID Principles (Mandatory)

- **SRP — Single Responsibility**: Every class/module has one reason to change. Split when a class serves multiple actors.
- **OCP — Open/Closed**: Extend behavior via abstractions (interfaces, strategy, decorator) — never modify stable code to add features.
- **LSP — Liskov Substitution**: Subtypes must be substitutable for their base without breaking callers. Avoid weakening preconditions or strengthening postconditions.
- **ISP — Interface Segregation**: Clients depend only on methods they use. Prefer several focused interfaces over one fat interface.
- **DIP — Dependency Inversion**: High-level modules depend on abstractions, not concretions. Inject dependencies; never instantiate infrastructure inside domain logic.

### Design Principles

- **KISS/DRY/YAGNI**: Favor readability. Abstract repeated logic (3+ occurrences). No speculative code.
- **Composition over Inheritance**: Prefer composing behaviors via interfaces and delegation over deep class hierarchies.
- **Law of Demeter**: Objects talk only to immediate collaborators — avoid chained accessors (`a.b.c.d`).
- **Naming**: Intention-revealing (`isUserAuthenticated` > `checkUser`). Follow language casing.

### Quality Attributes (Architectural Drivers)

Every design decision must weigh these attributes:

- **Maintainability**: Code readable and changeable with minimal risk. Small units, clear boundaries.
- **Reusability**: Extract shared logic into interfaces/modules usable across features/projects.
- **Testability**: Depend on abstractions so units can be tested in isolation with mocks/stubs.
- **Scalability**: Design for horizontal/vertical growth; stateless services, async I/O, bounded contexts.
- **Extensibility**: New behavior via plugins, strategies, or event hooks — not by editing existing code (OCP).

## 🧹 Code Hygiene

- **Size Limits**: Functions < 30 lines. Services < 600 lines. Utils < 400 lines.
- **Early Returns**: Use guard clauses to prevent deep nesting.
- **Comments**: Explain **why**, not **what**. Refactor instead of commenting bad code.
- **Sanitization**: Validate all external inputs.

## 🔧 Common Design Patterns (Apply When Appropriate)

- **Repository**: Abstract data access behind an interface — decouples domain from persistence.
- **Strategy**: Encapsulate interchangeable algorithms behind a common interface.
- **Factory**: Centralize complex object creation; keep clients unaware of concrete types.
- **Observer/Event Bus**: Decouple producers from consumers via publish-subscribe.
- **Adapter/Facade**: Simplify or translate third-party APIs into domain-friendly interfaces.
- **Decorator**: Add behavior to objects dynamically without modifying their class.

> Cross-reference: See **common/system-design** for system-level architectural patterns and quality attributes.

## 🚫 Anti-Patterns

- **Hardcoded Constants**: Use named config/constants.
- **Deep Nesting**: Avoid "Pyramid of Doom".
- **Global State**: Prefer dependency injection.
- **Empty Catches**: Always handle, log, or rethrow.
- **God Class**: Classes with 10+ responsibilities — split by actor/concern.
- **Tight Coupling**: Direct instantiation of dependencies — use DI and interfaces.
- **Leaky Abstractions**: Exposing implementation details (DTOs, ORM entities) across boundaries.
