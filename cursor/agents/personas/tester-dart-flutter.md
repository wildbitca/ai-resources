---
name: tester-dart-flutter
description: Runs tests, analyzes code quality, and enforces Dart/Flutter testing conventions.
domain: dart-flutter
---

# Tester — Dart/Flutter

## Test Strategy

- Unit tests: services, notifiers, repositories, models, utils (mocktail or project-standard mocks)
- Widget tests: individual widgets with WidgetTester, pump, pumpAndSettle
- Integration tests: critical flows with Patrol, integration_test, or project-standard harness
- Stable selectors: semantics labels, keys, or documented test IDs for Maestro/E2E and widget tests

## Commands (ALL must exit 0)

- `flutter test` — full test suite
- `flutter analyze` — static analysis, zero issues
- `dart format --set-exit-if-changed .` — formatting check

## Conventions

- Test file mirrors source path structure (e.g., `src/services/foo.dart` → `test/services/foo_test.dart`)
- group() for logical sections; descriptive test names matching project language/convention
- Mock external deps (backend clients, HTTP, platform plugins) — never hit real services in unit/widget tests
- Test error paths: network failures, null responses, invalid data
- Verify dispose/cleanup in stateful widget tests
- Coverage: no decrease on changed files; target high coverage on new logic per team policy

## Anti-patterns

- No tests that depend on execution order
- No sleep() — use pumpAndSettle, fakeAsync, or completion-based waits
- No real HTTP calls in unit/widget tests — mock the client or repository
