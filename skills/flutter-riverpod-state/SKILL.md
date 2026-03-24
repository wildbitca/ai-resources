---
name: flutter-riverpod-state
description: Applies Riverpod patterns for state, Notifiers, state classes with copyWith, and Result-based async handling. Use when adding or refactoring Riverpod providers, state classes, or async logic in Notifiers.
triggers: "**/*.dart, riverpod, provider, notifier, state management, copyWith, StateNotifier, AsyncNotifier, ConsumerWidget, ref.watch"
---

# Flutter Riverpod State

## Provider Choice

- Use **Riverpod** for state. Prefer `NotifierProvider` over `StateProvider` when the state has logic.
- Use `NotifierProvider` for stateful logic; `Provider` for stateless services and dependencies.

## Naming

- Services: `{serviceName}Provider` (e.g. `profileServiceProvider`).
- State: `{featureName}Provider` (e.g. `currentProfileProvider`, `activeProfileProvider`).
- Notifier classes: `{FeatureName}Notifier` (e.g. `ProfileNotifier`).

## State Class Shape

State should be immutable and include:

- Data (nullable when loading).
- `isLoading` (bool).
- `hasError` (bool).
- `errorMessage` (String?).

Use `copyWith` for every state update.

```dart
class FeatureState {
  const FeatureState({
    this.data,
    this.isLoading = false,
    this.hasError = false,
    this.errorMessage,
  });
  final DataType? data;
  final bool isLoading;
  final bool hasError;
  final String? errorMessage;

  FeatureState copyWith({
    DataType? data,
    bool? isLoading,
    bool? hasError,
    String? errorMessage,
  }) {
    return FeatureState(
      data: data ?? this.data,
      isLoading: isLoading ?? this.isLoading,
      hasError: hasError ?? this.hasError,
      errorMessage: errorMessage ?? this.errorMessage,
    );
  }
}
```

## Async and Result<T>

- All async work in Notifiers should use a `Result<T>` (or equivalent) with `Success` and `Failure`.
- Set `isLoading: true` before the async call; in the handler, set `isLoading: false` and either `data` + `hasError: false` or `hasError: true` + `errorMessage`.
- Use pattern matching (`switch` or `when`) on `Result`. Use helpers like `result.when`, `result.isSuccess`, `result.dataOrNull`, `result.errorOrNull` if available.
