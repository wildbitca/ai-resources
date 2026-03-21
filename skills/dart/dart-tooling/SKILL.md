---
name: dart-tooling
description: "Standards for analysis, linting, formatting, and automation. Use when configuring analysis_options.yaml, dart fix, dart format, or build_runner in Dart projects. (triggers: analysis_options.yaml, pubspec.yaml, build.yaml, analysis_options, lints, format, build_runner, cider, husky)"
---

# Tooling & CI

## **Priority: P1 (HIGH)**

Standards for code quality, formatting, and generation.

## Implementation Guidelines

- **Linter**: Use `analysis_options.yaml`. Enforce `always_use_package_imports` and `require_trailing_commas`.
- **Formatting**: `dart format . --line-length 80 --set-exit-if-changed`. Run on every commit.
- **DCM** (Dart Code Metrics): 475+ rules, metrics, unused code/file detection.
  - Install: `dart pub global activate dcm`
  - Analysis: `dcm analyze lib --fatal-style --fatal-performance --fatal-warnings`
  - Unused code: `dcm check-unused-code lib --no-exclude-overridden`
  - Unused files: `dcm check-unused-files lib`
  - Dependencies: `dcm check-dependencies lib`
  - Max cyclomatic complexity: 15.
- **Build Runner**: Always use `--delete-conflicting-outputs` with code generation.
- **CI Pipeline**: All PRs MUST pass `analyze`, `dcm`, `format`, `test`, and `security` steps.
- **Imports**: Group imports: `dart:`, `package:`, then relative.
- **Documentation**: Use `///` for public APIs. Link symbols using `[Class]`.
- **Security Scanning**:
  - `trivy fs --scanners vuln pubspec.lock` (dependency CVEs)
  - SonarQube with Dart plugin or Flutter Security Analyser (OWASP MASVS)
  - `gitleaks detect --source . --verbose` (secret detection)
- **Pre-commit**: Keep `lefthook.yml` in sync with analyze/format/dcm/security commands.

### CI Quality Gate (Minimum)

```bash
flutter analyze --fatal-infos --fatal-warnings
dcm analyze lib --fatal-style --fatal-performance
dart format . -l 80 --set-exit-if-changed
gitleaks detect --source . --verbose --redact
trivy fs --scanners vuln pubspec.lock --severity HIGH,CRITICAL --exit-code 1
flutter test
```

## Code

```yaml
# analysis_options.yaml
analyzer:
  errors:
    todo: ignore
    missing_required_param: error
linter:
  rules:
    - prefer_single_quotes
    - unawaited_futures
```

## Related Topics

language | testing


## 🚫 Anti-Patterns

- Do NOT use standard patterns if specific project rules exist.
- Do NOT ignore error handling or edge cases.
