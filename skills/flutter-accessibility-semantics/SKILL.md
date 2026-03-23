---
name: flutter-accessibility-semantics
description: Enforces semantic identifiers for E2E tests, Maestro, and accessibility in Flutter. Use when adding testable widgets, implementing E2E flows, or improving accessibility.
---

# Flutter Accessibility & Semantic IDs

## Semantic Identifiers

- Use semantic identifiers for interactive widgets so E2E tools (e.g. Maestro) and widget tests can find them reliably.
- **REQUIRED:** Wrap interactive widgets with `Semantics(identifier: semanticId, button: true, ...)` (or `Semantics` with appropriate properties).
- IDs should be stable, locale-independent strings (e.g. `login_google`, `profile_edit_button`).

## Centralized ID Registry

- **REQUIRED:** Centralize all semantic IDs in a config class or constants (e.g. `SemanticIdsConfig`, `AppConstants.semantic`).
- Add new IDs to the registry when creating testable screens or widgets.
- **BENEFIT:** Avoids duplicate IDs, makes refactors easier, documents which widgets are testable.

### Example

```dart
// Config
class SemanticIdsConfig {
  const SemanticIdsConfig();
  final String loginGoogle = 'login_google';
  final String loginApple = 'login_apple';
  final String profileEditButton = 'profile_edit_button';
}

// Usage
Semantics(
  identifier: AppConstants.semantic.loginGoogle,
  button: true,
  child: ElevatedButton(...),
)
```

## Maestro / E2E

- Maestro flows reference IDs by string value: `tapOn: { id: login_google }`.
- E2E flows in `e2e/` (e.g. `Login.yaml`) depend on semantic IDs being present.
- **REQUIRED:** When adding a new flow step, ensure target widgets have semantic identifiers.

## Accessibility

- Semantic IDs also improve screen reader support when combined with `label`, `hint`, etc.
- Use `Semantics` with `button: true`, `link: true`, or other properties as appropriate for the widget role.

## Workflow

1. Define a new ID in the central config.
2. Wrap the target widget with `Semantics(identifier: config.xyz, ...)`.
3. Add or update Maestro/E2E flow if applicable.
4. Run E2E tests to verify flows pass.
