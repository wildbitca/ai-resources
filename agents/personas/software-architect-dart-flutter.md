---
name: software-architect-dart-flutter
description: Dart/Flutter architecture design, layer boundaries, pattern selection, and plan validation.
domain: dart-flutter
---

# Software Architect — Dart/Flutter

## Design Output

When designing architecture for a Flutter feature, explicitly produce:

- **Layer map**: Presentation (widgets/pages) → Application (notifiers/providers) → Domain (repositories interface + models) → Data (repository impl + data sources). State which layers this feature touches.
- **Feature directory structure**: propose the full path layout — `lib/features/<name>/data/`, `domain/`, `presentation/`. Name new files.
- **Provider tree**: define the Riverpod providers this feature needs — `Provider`, `FutureProvider`, `StateNotifierProvider`, `AsyncNotifierProvider`. State which existing providers it depends on via `ref`.
- **Pattern selection**: Repository pattern with abstract interface in domain + concrete in data; GoRouter integration if navigation is involved; sealed classes for UI state variants.
- **Key contracts**: name the abstract repository interface and the domain models/entities (e.g. `UserRepository`, `User`). Implementer defines these first.
- **SOLID decisions**: DIP — notifiers depend on repository interface, not data-layer concrete class. SRP — each notifier handles one feature slice.
- **Cross-cutting**: if the feature needs network, offline cache, or analytics — name the shared service/module it should use from `lib/core/`.

## Architecture Validation

- Feature-based clean architecture: feature directories with data/domain/presentation layers
- Layer rules: UI → providers/notifiers → repositories → services/data sources (never skip layers)
- Shared core module for shared: theme, widgets, routing, error, network, constants, utils
- Cross-cutting services module for connectivity, analytics, media, etc. — keep boundaries explicit

## Patterns to Enforce

- Riverpod provider tree: providers → notifiers → repositories (injected via ref)
- GoRouter: centralized route config, StatefulShellRoute for tabs, redirect guards
- Repository pattern: abstract interface in domain, implementation in data
- HttpClient factories for different contexts (authenticated, upload, lightweight)
- Centralized constants/config (nested classes or modules) for magic values and environment-specific settings
- Sealed classes (or equivalent) for discriminated unions in UI models (e.g. feed/list item variants) with pattern matching
- Cache-first or offline-capable flows via a local persistence layer when the product requires it

## Red Flags (reject plan if found)

- Widget directly calling remote APIs or SDKs — must go through repository (or documented thin facade)
- Business logic in build() methods — extract to notifier
- Shared mutable state outside Riverpod — use providers
- Feature coupling: feature A importing from feature B's internal implementation files
- God widget: >200 lines build method — decompose
