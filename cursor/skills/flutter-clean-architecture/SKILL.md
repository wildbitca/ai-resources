---
name: flutter-clean-architecture
description: Enforces feature-based modular structure, dependency rules, and widget composition (avoid Widget Hell) for Flutter apps. Use when organizing or adding features, refactoring large build methods, or deciding where to put new code.
---

# Flutter Clean Architecture

## SOLID Foundation

This skill enforces SOLID through feature-based modular structure:

- **SRP**: Each feature owns one business domain; each widget has one visual responsibility.
- **OCP**: New features = new directories; new UI = new widget classes — never modify stable widgets to add behavior.
- **LSP**: Widget substitution (e.g., swapping `FeedCard` for `AdCard`) works without breaking the parent.
- **ISP**: Pass only needed data to widgets via constructor — no "pass the whole model" anti-pattern.
- **DIP**: Features depend on `core/` interfaces and `services/` abstractions — never on concrete implementations.

## Quality Attributes

| Attribute | How Enforced |
|-----------|-------------|
| **Maintainability** | Feature isolation + small widget classes = minimal blast radius for changes |
| **Reusability** | Shared widgets in `core/`; domain interfaces reusable across features |
| **Testability** | Small focused widgets are trivially testable; logic in Notifiers/services can be mocked |
| **Scalability** | Feature directories enable parallel development by multiple developers/teams |
| **Extensibility** | New behavior via new widget classes, new Notifiers — existing code stays closed |

## Structure

- Organize by **feature**, not by global layers. Each feature (auth, feed, profile, upload, etc.) is self-contained.
- **PROHIBITED:** One big global `models/`, `screens/`, `widgets/` that mixes all features.

## Typical Layout

```
lib/
├── core/
│   ├── constants/
│   ├── network/
│   ├── theme/
│   └── error/
├── features/
│   ├── auth/
│   ├── feed/
│   ├── profile/
│   └── upload/
├── services/
└── main.dart
```

- `core/`: shared constants, HTTP, theme, error types, base widgets.
- `features/<name>/`: everything for that feature (screens, widgets, providers, models, use cases).
- `services/`: cross-cutting or external services (ads, push, analytics, etc.).

## Dependencies

- **Dependency Inversion (DIP):** Inner layers must not depend on outer layers. Features may depend on `core` and `services`; `core` should not depend on a specific feature.
- Keep feature modules independent of each other where possible; use `core` or shared interfaces for cross-feature contracts (ISP).

## Separation

- UI and business logic stay separate (SRP). Put logic in Notifiers, services, or use cases; widgets only build UI and dispatch actions.
- Apply **Repository** for data access; **Strategy** for behavioral variation; **Factory** for complex object creation; **Observer/Event Bus** for cross-feature communication; **Adapter** for third-party SDK wrapping.

> Cross-reference: See **common/best-practices** for SOLID enforcement and **common/system-design** for architectural patterns.

---

# Avoiding Widget Hell (Massive Build Methods)

**Widget Hell** is when a single `build` method spans hundreds of lines. It hurts readability, performance, and debugging. The Flutter team and community consensus: **favor small Widget classes over helper functions.**

## 1. Classes vs Helper Functions

Using a local function like `_buildHeader()` to wrap a chunk of UI is an **anti-pattern**.

| Feature | Helper functions (`_buildX`) | Small widget classes |
|--------|-----------------------------|------------------------|
| **Reusability** | Limited to current file | Usable anywhere in the app |
| **Rebuild optimization** | **None.** Entire parent rebuilds | **High.** Flutter can skip rebuilding sub-widgets |
| **Context access** | Inherits parent context (risky) | Has its own `BuildContext` |
| **Hot reload** | Can fail to track local changes | Reliable and precise |
| **Performance** | Higher overhead during deep tree diffing | Faster due to short-circuit rebuilding |

**REQUIRED:** Use small `StatelessWidget` (or `StatefulWidget` when needed) classes with `const` constructors. **PROHIBITED:** Helper functions that build UI (e.g. `_buildHeader()`, `_buildRow()`).

## 2. Why Small Classes Win

- **Efficiency:** A function is part of the parent’s build; Flutter re-executes it every time the parent rebuilds. A **class** (especially `const`) can be skipped when parameters haven’t changed.
- **Const advantage:** With `const`, Flutter may not re-evaluate that subtree at all. You cannot get this with functions.
- **Pitfall of functions:** Calling a function inside `build` runs that code on every parent refresh — unnecessary CPU work with animations or heavy state.

## 3. Refactoring Large Build Methods

1. **Identify logical segments:** e.g. custom search bar, profile card, navigation row.
2. **Extract to a `StatelessWidget`:** Use IDE refactor (e.g. “Extract Widget”, Cmd+Shift+R or Alt+Enter). Name the widget clearly.
3. **Pass data, not state:** Pass needed data via the constructor. Avoid passing `ChangeNotifier` or heavy logic unless necessary.
4. **Keep it focused:** One widget, one responsibility. If the extracted widget is still 200+ lines, split again.

## 4. When Are Functions Acceptable?

Helper functions are acceptable only when:

- The UI is **very simple** (e.g. a tiny padding wrapper).
- It does **not** use `BuildContext` for theme, localization, or navigation.
- The code is private and will **never** be reused elsewhere.

**Rule of thumb:** If in doubt, make it a class.

## 5. Linter Support

Enable in `analysis_options.yaml`:

- `prefer_const_constructors` — encourages `const` widgets.
- `sort_child_properties_last` — keeps child last for readability and consistency.

These nudge toward modular, optimized widget trees.
