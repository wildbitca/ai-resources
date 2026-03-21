---
name: common-system-design
description: "Universal architectural standards for robust, scalable systems. Use when designing new features, evaluating architecture, or resolving scalability concerns. (triggers: architecture, design, system, scalability)"
---

# System Design & Architecture Standards

## **Priority: P0 (FOUNDATIONAL)**

## SOLID Foundation (Mandatory)

Every architectural decision must be traceable to SOLID:

- **SRP**: Each module/service owns one bounded context. Split when multiple actors drive changes.
- **OCP**: Systems extend via plugins, strategies, or event hooks — not by modifying stable modules.
- **LSP**: Interchangeable implementations (e.g., cache backends, auth providers) without breaking consumers.
- **ISP**: Define narrow, role-specific interfaces per consumer — no "God interfaces".
- **DIP**: High-level policy depends on abstractions; infrastructure implements those abstractions.

> Cross-reference: See **common/best-practices** for code-level SOLID enforcement.

## Architectural Principles

- **SoC**: Divide into distinct sections per concern.
- **SSOT**: One source, reference elsewhere.
- **Fail Fast**: Fail visibly when errors occur.
- **Graceful Degradation**: Core functional even if secondary fails.
- **Composition over Inheritance**: Build systems from small, composable modules — not deep hierarchies.

## Quality Attributes (Architectural Drivers)

Design decisions must explicitly optimize for:

| Attribute | Measure | Enabled By |
|-----------|---------|------------|
| **Maintainability** | Change risk, code churn | SRP, SoC, small modules |
| **Reusability** | Shared modules across features/projects | ISP, DIP, abstraction layers |
| **Testability** | Unit test coverage, mock ease | DIP, DI, pure domain logic |
| **Scalability** | Load capacity, horizontal growth | Statelessness, async I/O, bounded contexts |
| **Extensibility** | Add features without editing existing code | OCP, Strategy, event-driven |
| **Portability** | Swap infrastructure (DB, cloud, framework) | DIP, Clean/Hexagonal architecture |

## Modularity & Coupling

- **High Cohesion**: Related functionality in one module.
- **Loose Coupling**: Use interfaces for communication.
- **DI**: Inject dependencies, don't hardcode.
- **Bounded Contexts**: Define clear module boundaries; cross-module communication only via public interfaces/events.

## Architectural Patterns

- **Layered**: Presentation → Logic → Data. Strict dependency direction.
- **Clean/Hexagonal (Ports & Adapters)**: Core logic independent of frameworks. Infrastructure plugs in via interfaces (DIP).
- **Event-Driven**: Async communication between decoupled components via publish-subscribe.
- **CQRS**: Separate read and write models when query and command complexity diverge.
- **Statelessness**: Favor stateless services for horizontal scaling and testability.

## Design Patterns (Apply When Appropriate)

- **Repository**: Abstract data access behind domain interfaces.
- **Strategy**: Encapsulate interchangeable algorithms.
- **Factory/Abstract Factory**: Centralize object creation; hide concrete types.
- **Observer**: Decouple event producers from consumers.
- **Adapter/Facade**: Translate or simplify external APIs at system boundaries.
- **Decorator**: Layer cross-cutting concerns (logging, caching, auth) without modifying core logic.

## Distributed Systems

- **CAP**: Trade-off Consistency/Availability/Partition tolerance. See [CAP & Consistency Patterns](references/distributed-systems.md).
- **Idempotency**: Operations repeatable without side effects. See [Idempotency Patterns](references/distributed-systems.md#idempotency).
- **Circuit Breaker**: Fail fast on failing services. See [Resilience Patterns](references/resilience-patterns.md).
- **Eventual Consistency**: Design for async data sync. See [CAP & Consistency Patterns](references/distributed-systems.md#eventual-consistency).

## Documentation & Evolution

- **Design Docs**: Write specs before major implementations.
- **Versioning**: Version APIs/schemas for backward compatibility.
- **Extensibility**: Use Strategy/Factory for future changes.

## References

- [Distributed Systems & CAP Theorem](references/distributed-systems.md)
- [Resilience Patterns (Circuit Breaker, Bulkhead, Retry)](references/resilience-patterns.md)


## 🚫 Anti-Patterns

- Do NOT use standard patterns if specific project rules exist.
- Do NOT ignore error handling or edge cases.
