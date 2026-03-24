---
name: flutter-code-quality
description: Enforces Dart/Flutter code quality via analyzer, cleanup, and dependency rules. Use when completing Flutter coding tasks, before considering work done, or when the user asks to run analyzer or fix lint issues.
triggers: "**/*.dart, flutter analyze, dart analyze, lint, analyzer, fix warnings, code quality, unused imports, deprecated API"
---

# Flutter Code Quality

## Analyzer Enforcement

After every coding task:

1. Run `flutter analyze` on the entire project (use `flutter analyze`, not `dart analyze`).
2. Fix ALL issues: errors, warnings, and info. Treat warnings as errors.
3. Remove unused imports; replace deprecated APIs immediately.
4. Re-run analyzer to verify zero issues before considering the task complete.

**PROHIBITED:** Committing with errors or warnings; ignoring warnings or info without justification.

## Workflow

```
Complete coding task → Run flutter analyze → Fix all reported issues → Re-run to verify → Done
```

## Dependency Management

- Ensure `pubspec.yaml` dependencies are null-safe and up-to-date.
- Align versions with the project's SDK and existing dependencies.
- Prefer project's chosen state solution (e.g. Riverpod over `provider` when that's the convention).

## Code Cleanup (When Performing "Cleanup Code")

For Dart/Flutter:

1. **Comments:** Remove all except doc/legal/complex-algorithm/config-explaining.
2. **Unused:** Remove unused imports, types, functions, variables, parameters, files. Use `flutter analyze`.
3. **Linters:** Fix all analyzer/linter issues; aim for zero before done.
4. **Config:** Remove unused env vars, `AppConfig`/`ApiConstants` entries if applicable.
5. **Duplicates:** Consolidate into shared utils.
6. **Final step:** Run `flutter analyze` again.

For other languages, use the project's analyzer (e.g. `tsc --noEmit`, `shellcheck` for Bash) and the same cleanup principles.

## Config and i18n (Quality Consistency)

- **REQUIRED:** Use config files (e.g. `config/dev.env`, `prd.env`) and centralized `AppConfig`/`ApiConstants` for environment values; never hardcode URLs or keys.
- **REQUIRED:** For i18n: ARB files (e.g. `app_en.arb`, `app_es.arb`) and `flutter gen-l10n`; user-facing strings via `AppLocalizations` or equivalent.
- **BENEFIT:** Maintainable, analyzable, localized codebase.

## Quick Checklist

- [ ] `flutter analyze` run and passes with zero issues
- [ ] No unused imports or deprecated APIs
- [ ] Dependencies in `pubspec.yaml` are coherent and up-to-date
