---
name: flutter-dart-standards
description: Enforces Dart type safety, immutability, naming, and widget composition in Flutter projects. Use when writing or reviewing Dart/Flutter code.
---

# Flutter Dart Standards

## Type Safety

- **PROHIBITED:** `dynamic`.
- **REQUIRED:** Explicit types for variables and function signatures. Use inference only when the type is obvious from context.

## Immutability

- Mark custom widgets and models `@immutable` when applicable.
- Use `final` and `copyWith` for state updates. Prefer immutable data classes.

## Naming

| Element           | Convention | Example                |
|-------------------|------------|------------------------|
| Classes           | PascalCase | `VideoFeedController`  |
| Functions/vars    | camelCase  | `getVideoFeed()`       |
| Files/directories | snake_case | `feed_controller.dart` |

### Acronym Capitalization

- **REQUIRED:** Acronyms in identifiers MUST be all capitals: FCM, SQL, API, URL, ID, JSON.
- **EXAMPLES:** `FCMTokenService` not `FcmTokenService`; `sendFCM` not `sendFcm`; SQL not Sql.
- **EXCEPTION:** In camelCase when the acronym starts the identifier, lowercase is acceptable: `fcmToken`, `apiKey`.

## Development Language

- **REQUIRED:** All development resources MUST be in English: code identifiers, file names, variable names, class names, DB columns, API endpoints, config keys, commit messages, PR titles, docs.
- **EXCEPTION:** User-facing strings (labels, messages, UI text) use i18n (ARB files, AppLocalizations); content language is determined by locale, not this rule.
- **BENEFIT:** Consistent codebase for global collaboration and tooling compatibility.

## Performance

- Use `const` constructors for widgets whenever possible to reduce rebuilds.

## Widgets

- **PREFERRED:** Composition over inheritance.
- **AVOID:** Deep class hierarchies.
- **CREATE:** Small, focused widgets and compose them for better testability and maintenance.
