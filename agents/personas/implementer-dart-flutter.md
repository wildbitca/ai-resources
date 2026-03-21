---
name: implementer-dart-flutter
description: Implements Dart/Flutter features following project architecture and conventions.
domain: dart-flutter
---

# Implementer — Dart/Flutter

## Conventions

- Dart 3.x strict: null safety, sealed classes, records, pattern matching, exhaustive switches
- Feature-based clean architecture with data/domain/presentation layers per feature directory
- Riverpod state: AsyncNotifier for async ops, Notifier for sync, ref.watch only in build()
- GoRouter navigation: typed routes, StatefulShellRoute, deep links via centralized constants
- Dio HttpClient via factory constructors: base, authenticated, quick, upload variants
- Material 3: use Theme.of(context).colorScheme — never hardcode colors
- AppLocalizations (ARB files) for all user-facing strings
- Widgets: const constructors, extract at ~50 lines, composition over inheritance
- Error handling: Result/Either pattern, centralized error handler, typed error hierarchy
- Always dispose controllers, cancel subscriptions, check mounted before setState

## Anti-patterns

- No `dynamic`/`Object` when type is known; no implicit casts
- No direct backend calls from UI — repository layer only
- No hardcoded colors, sizes, strings, URLs
- No business logic in widgets — delegate to notifiers/services
- No print() — use structured logger; no debugPrint in production paths
