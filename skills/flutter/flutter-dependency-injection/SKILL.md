---
name: flutter-dependency-injection
description: "Standards for automated service locator setup using injectable and get_it. Use when configuring dependency injection with injectable and get_it in Flutter. (triggers: **/injection.dart, **/locator.dart, GetIt, injectable, singleton, module, lazySingleton, factory)"
---

# Dependency Injection

## **Priority: P1 (HIGH)**

Automated class dependency management using `get_it` and `injectable`.

## SOLID Foundation

DI is the primary mechanism for enforcing SOLID at the wiring level:

- **SRP**: Each module/registration file owns one bounded context; split registration by feature, not one monolithic file.
- **OCP**: Swap implementations (prod, mock, staging) without modifying consumers — just change the registration.
- **LSP**: All registered implementations must honor the full interface contract — e.g., a `MockAuthRepository` must behave like `AuthRepository` for all callers.
- **ISP**: Register narrow, role-specific interfaces (`as: IAuthReader`, `as: IAuthWriter`) rather than monolithic service contracts.
- **DIP**: High-level modules (BLoCs, use cases) depend on abstractions (interfaces); `get_it` wires concrete implementations at runtime.

## Quality Attributes

| Attribute | How DI Delivers It |
|-----------|-------------------|
| **Maintainability** | Centralized wiring; change one registration, not N call sites |
| **Reusability** | Same interface, multiple implementations across features/environments |
| **Testability** | Replace any dependency with a mock in tests — no concrete coupling |
| **Scalability** | Feature-scoped registration modules enable parallel development |
| **Extensibility** | New implementations registered without touching existing code (OCP) |

## Structure

```text
core/injection/
├── injection.dart # Initialization & setup
└── modules/ # Third-party dependency modules (Dio, Storage)
```

## Implementation Guidelines

- **Automated Registration**: Use `@injectable` annotations; avoid manual registry calls.
- **@LazySingleton**: Default for repositories, services, and data sources (init on demand).
- **@injectable (Factory)**: Default for BLoCs to ensure state resets on every request.
- **Abstract Injection**: Always register implementations as abstract interfaces (`as: IService`).
- **Modules**: Use `@module` for registering third-party instances (e.g., `Dio`, `SharedPreferences`).
- **Constructor Injection**: Use mandatory constructor parameters; `injectable` resolves them.
- **Test Mocks**: Swap implementations in `setUp` using `getIt.registerLazySingleton` for testing.

## Reference & Examples

For module configuration and initialization templates:
See [references/REFERENCE.md](references/REFERENCE.md).

## Related Topics

layer-based-clean-architecture | testing

> Cross-reference: See **common/best-practices** for SOLID enforcement and **common/system-design** for architectural patterns.

## 🚫 Anti-Patterns

- Do NOT use standard patterns if specific project rules exist.
- Do NOT ignore error handling or edge cases.
- Do NOT register concrete classes without an interface — always use `as: IAbstraction` (DIP).
- Do NOT create a single module file registering everything — split by feature or bounded context (SRP).
