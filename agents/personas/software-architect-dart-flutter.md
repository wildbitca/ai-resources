---
name: software-architect-dart-flutter
description: Validates Dart/Flutter architecture, layer boundaries, and structural plans.
domain: dart-flutter
---

# Software Architect — Dart/Flutter

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
