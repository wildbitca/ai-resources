# Architecture Audit Patterns & Remediation

This reference provides detailed patterns and strategies for remediating structural issues identified during an Architecture Audit.

## 🎯 SOLID Violation Patterns & Remediation

### SRP Violation: Multi-Responsibility Class

**Pattern**: A class handles authentication, validation, logging, and data access simultaneously.
**Remediation**: Extract each responsibility into a dedicated class. One actor, one reason to change.

### OCP Violation: Modification-Driven Extension

**Pattern**: Adding a new payment method requires editing `PaymentService` switch/if-else chains.
**Remediation**: Introduce **Strategy Pattern** — each payment method implements a `PaymentStrategy` interface. Register new strategies without touching existing code.

### LSP Violation: Broken Subtype Contract

**Pattern**: A `ReadOnlyRepository` extends `Repository` but throws on `save()`.
**Remediation**: Split into separate interfaces (`IReader`, `IWriter`). Clients depend only on what they need (also fixes ISP).

### ISP Violation: Fat Interface

**Pattern**: `IUserService` with 20+ methods forces all implementors to provide unused methods.
**Remediation**: Split into role-specific interfaces (`IUserReader`, `IUserWriter`, `IUserAdmin`).

### DIP Violation: Concrete Infrastructure in Domain

**Pattern**: Domain use case imports `package:dio` or `package:hive` directly.
**Remediation**: Define an interface in the domain layer. Infrastructure implements it. Inject via DI container.

## 🏗️ Structural Patterns

### 1. The "Logic-Heavy UI" (Web/Mobile)

**Pattern**: Components or Widgets containing complex business logic, direct API calls, or heavy state manipulation.
**Remediation**:

- **Web**: Extract complex `useEffect` or `useState` chains into custom hooks (`useFeatureLogic`).
- **Mobile**: Move business logic to BLoC, Provider, or a dedicated Service class.
- **SOLID**: Enforces SRP (UI renders; logic orchestrates) and DIP (UI depends on abstractions, not API clients).

### 2. The "God Service" (Backend)

**Pattern**: A single Service class exceeding 1,500 lines handling multiple distinct entities or responsibilities.
**Remediation**:

- Implement the **Single Responsibility Principle**.
- Extract sub-domains into specific services (e.g., `UserService` → `UserAuthService`, `UserProfileService`).
- **SOLID**: Directly addresses SRP; use ISP to define separate interfaces for each sub-service.

### 3. Database Leakage (Universal)

**Pattern**: Domain entities or DTOs containing transformation logic that depends on specific database drivers or ORM features.
**Remediation**:

- Use a **Data Mapper** pattern.
- Ensure the Domain layer is agnostic of the persistence layer.
- **SOLID**: Enforces DIP (domain doesn't know about infrastructure) and SRP (mapper has one job).

### 4. Switch/If-Else Chain (Universal)

**Pattern**: Long conditional chains dispatching behavior by type (e.g., `if type == "A" ... else if type == "B" ...`).
**Remediation**:

- Apply **Strategy** or **Polymorphism**. Each type implements a shared interface.
- **SOLID**: Enforces OCP (new types = new classes, no modification) and LSP (each implementation is substitutable).

### 5. Tight Bidirectional Coupling (Universal)

**Pattern**: Module A imports Module B and Module B imports Module A.
**Remediation**:

- Introduce an **Observer/Event Bus** or shared interface in a third module.
- **SOLID**: Enforces DIP (both depend on abstractions) and SRP (decoupled responsibilities).

## 🔧 Design Patterns for Remediation

| Pattern | When to Apply | SOLID Principle |
|---------|--------------|-----------------|
| **Repository** | Data access scattered across layers | DIP, SRP |
| **Strategy** | Behavioral variation via conditionals | OCP, LSP |
| **Factory** | Complex object creation with conditionals | OCP, SRP |
| **Observer/Event Bus** | Tight coupling between modules | DIP, SRP |
| **Adapter** | External SDK used directly in domain | DIP, ISP |
| **Facade** | Complex subsystem exposed without simplification | ISP, SRP |
| **Decorator** | Cross-cutting concerns (logging, caching) mixed into business logic | OCP, SRP |
| **Data Mapper** | DTOs leaked into domain/UI | DIP, SRP |

## 🛠️ Recovery Strategies

| Finding            | Immediate Action                             | Long-term Fix                                               | SOLID |
| ------------------ | -------------------------------------------- | ----------------------------------------------------------- | ----- |
| **Monolith File**  | Extract helper functions to private methods. | Break into smaller, atomic components or modules.           | SRP |
| **Logic Leakage**  | Move logic to a temporary helper file.       | Redesign the service/hook layer to own the logic correctly. | SRP, DIP |
| **Duplicate Core** | Mark legacy version as `@deprecated`.        | Consolidate and migrate usage to the standard version.      | DRY, SSOT |
| **Fat Interface**  | Add `@deprecated` to unused methods.         | Split into role-specific interfaces.                        | ISP |
| **Concrete Coupling** | Wrap in adapter/facade immediately.       | Define abstractions in domain; inject implementations.      | DIP, OCP |
