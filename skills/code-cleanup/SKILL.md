---
name: code-cleanup
description: Applies generic code cleanup (comments, unused code, empty dirs, linters, config, duplicates) for any language (Dart, Bash, Python, Java, Kotlin, Swift, TS/JS, etc.) and, for Flutter/Dart, a combined quality check. Use when the user asks to clean up code, remove dead code, empty dirs, reduce technical debt, or run a full project quality check.
triggers: "clean up, cleanup, dead code, unused code, remove comments, empty directories, technical debt, lint, refactor, unused imports, duplicates, code quality check"
---

# Code Cleanup (Any Language)

## Steps

1. **Comments:** Remove non-needed extra comments in all code files. Keep only: docblocks (API/docs), legal headers, complex-algorithm explanations, and config-explaining comments. See **Comment cleanup by language** below.
2. **Unused:** Remove unused imports, types, functions, variables, parameters, and files. Use the project's analyzer per language (see **By language** below).
3. **Empty directories:** Find empty dirs (including nested empty dirs). Remove them from the filesystem and from Git tracking (`git rm -r --cached <dir>` if tracked, then remove dir; or remove dir and `git add` the deletion). Re-check after file removals.
4. **Linters:** Fix all reported analyzer/linter issues; aim for zero before done.
5. **Config:** Remove unused env vars, `AppConfig`/`ApiConstants` (or equivalents), and unused entries in `wrangler.toml`, `config.toml`, etc.
6. **Duplicates:** Merge duplicated logic into shared utils or helpers.
7. **Final check:** Run the project's analyzer (or equivalent) again; confirm no empty dirs left.

## Prohibited: Code Formatting

**PROHIBITED:** Do NOT format code. This skill focuses on cleanup and quality checks, not formatting.

- **PROHIBITED:** Running `dart format`, `flutter format`, or any code formatter.
- **PROHIBITED:** Changing code style, indentation, spacing, or line breaks.
- **PROHIBITED:** Reorganizing imports beyond removing unused ones.
- **PROHIBITED:** Changing brace style, line length, or other formatting conventions.

**Allowed:** Only remove unused imports (not reorganize them), fix analyzer errors that require code changes (not style changes), and apply the quality checks listed below.

---

## Empty Directories

- **Check:** After removing unused files, find directories that are empty (including nested: e.g. `a/b/c` then `a/b` then `a` if all become empty).
- **Remove from filesystem:** Delete empty dirs (e.g. `rmdir` or `find ... -type d -empty -delete`; on Windows use equivalent).
- **Remove from Git:** If the dir was tracked (e.g. had a `.gitkeep` or was previously committed), stop tracking and remove: `git rm -r --cached <path>` then remove the dir; or remove the dir and stage the deletion with `git add -A` / `git add <path>`. Untracked empty dirs are not in Git; deleting
  them is enough.
- **Re-check:** After any file or dir removal, run the empty-dir check again until no empty dirs remain.

---

## Comment Cleanup (All Code Files)

Remove **non-needed** comments in every codebase. Apply to Dart, Bash, Python, Java, Kotlin, Swift, TypeScript/JavaScript, and other code files.

**Remove:**

- Redundant or obvious comments (e.g. `// increment i`, `# set variable`).
- Commented-out code blocks (dead code); delete the code instead of leaving it commented.
- Stale TODOs that are done or obsolete; leave only actionable TODOs.
- Noise comments (e.g. `// -----`, `# ====`, decorative separators with no info).
- Per-line comments that restate the next line.

**Keep:**

- **Docblocks / API docs:** `///`, `/** */`, `""" """`, `#` module/function doc, JSDoc, KDoc, etc.
- **Legal/copyright headers:** License, copyright, author at top of file.
- **Complex-algorithm comments:** Non-obvious logic, workarounds, references to specs/tickets.
- **Config-explaining comments:** Why a constant or env value is set (e.g. in config files).

| Language          | Docblock / keep style | Single-line | Block comment |
|-------------------|-----------------------|-------------|---------------|
| **Dart**          | `///`, `/** */`       | `//`        | `/* */`       |
| **Bash**          | `#` at block start    | `#`         | N/A           |
| **Python**        | `""" """`, `''' '''`  | `#`         | N/A           |
| **Java**          | `/** */` Javadoc      | `//`        | `/* */`       |
| **Kotlin**        | `/** */` KDoc, `///`  | `//`        | `/* */`       |
| **Swift**         | `///`, `/** */`       | `//`        | `/* */`       |
| **TS/JavaScript** | `/** */` JSDoc        | `//`        | `/* */`       |

When in doubt: if the comment does not document the public API, explain non-obvious logic, or provide legal/config context, remove it.

---

## By Language (Analysis, Formatting, Security)

Use the **strongest available tooling** per language. Fix all issues before done.

See [references/TOOLING-MATRIX.md](references/TOOLING-MATRIX.md) for the full tooling matrix with install commands and CI snippets.

| Language          | Static Analysis                                                    | Formatting               | Security / SAST                                              | Dep Audit                             |
|-------------------|--------------------------------------------------------------------|--------------------------|--------------------------------------------------------------|---------------------------------------|
| **Dart/Flutter**  | `flutter analyze --fatal-infos` + **DCM** (`dcm analyze`)          | `dart format . -l 80`    | SonarQube, Trivy (`pubspec.lock`), Flutter Security Analyser | `dart pub outdated`, Trivy SCA        |
| **TypeScript/JS** | `tsc --noEmit` + **ESLint v9** (strict, type-checked) or **Biome** | **Biome** or Prettier    | **Semgrep** (`auto`), `eslint-plugin-security`               | `npm audit --audit-level=high`, Trivy |
| **Angular**       | `ng lint` (**angular-eslint**)                                     | Biome or Prettier        | Semgrep, SonarQube                                           | `npm audit`, Trivy                    |
| **Bash**          | **ShellCheck**                                                     | `shfmt`                  | ShellCheck SC2* rules                                        | N/A                                   |
| **Python**        | **Ruff** + **Pyright**                                             | **Ruff** format or Black | **Bandit**, Semgrep                                          | `pip-audit`, Trivy                    |
| **Java**          | Build + **SpotBugs** + **ErrorProne**                              | google-java-format       | SpotBugs Find Security Bugs, Semgrep                         | OWASP Dep-Check                       |
| **Kotlin**        | **Detekt** + **ktlint**                                            | ktlint `--format`        | Detekt security ruleset, Semgrep                             | Gradle audit                          |
| **Swift**         | Xcode + **SwiftLint**                                              | **SwiftFormat**          | SwiftLint security rules                                     | `swift package audit`                 |
| **Dockerfile**    | **Hadolint**                                                       | N/A                      | **Trivy** image scan, **Dockle** CIS                         | Trivy                                 |
| **SQL**           | **sqlfluff**                                                       | sqlfluff fix             | Semgrep SQL-injection rules                                  | N/A                                   |

### Cross-Language (Apply to All Projects)

| Tool         | Purpose                       | Command                                       |
|--------------|-------------------------------|-----------------------------------------------|
| **Gitleaks** | Secret detection (150+ types) | `gitleaks detect --source . --verbose`        |
| **Trivy**    | CVE + misconfig + secrets     | `trivy fs . --scanners vuln,misconfig,secret` |
| **Semgrep**  | Pattern SAST (30+ langs)      | `semgrep scan --config auto .`                |

---

# Flutter/Dart: Combined Quality Check

When cleaning up or validating a **Flutter/Dart** project, after the base cleanup steps, run this combined check. Each item delegates to the corresponding skill for full rules; here we list the main verifiable points.

## 1. Code Quality — Apply: `flutter-code-quality`

- Run `flutter analyze`; fix all errors, warnings, and info. Treat warnings as errors.
- Remove unused imports (do NOT reorganize or format them); replace deprecated APIs.
- Check `pubspec.yaml`: null-safe, up-to-date, aligned with SDK and state solution (e.g. Riverpod).
- Re-run `flutter analyze` to confirm zero issues.
- **PROHIBITED:** Running `dart format` or `flutter format` — this skill does NOT format code.

## 2. Dart Standards — Apply: `flutter-dart-standards`

- No `dynamic`; explicit types where not obvious.
- Custom widgets/models: `@immutable`, `final`, `copyWith` for state.
- Naming: PascalCase (classes), camelCase (functions/vars), snake_case (files/dirs).
- Use `const` constructors where possible; composition over deep inheritance.

## 3. Clean Architecture — Apply: `flutter-clean-architecture`

- Feature-based layout: `lib/features/<name>/`; no global `models/` or `widgets/` mixing features.
- `core/` for shared code; `features/` self-contained; inner layers do not depend on outer.
- UI vs logic: logic in Notifiers/services; widgets build UI and dispatch.

## 4. Riverpod State — Apply: `flutter-riverpod-state`

- Prefer `NotifierProvider` for state with logic; `Provider` for stateless services.
- State: `data`, `isLoading`, `hasError`, `errorMessage`; updates via `copyWith`.
- Async in Notifiers: `Result<T>` with `Success`/`Failure`; pattern match; set loading before, clear in both branches.

## 5. Error Handling — Apply: `flutter-error-handling`

- Async services return `Result<T>`; no raw `throw` or `try/catch` without converting to `Result`.
- Central `ErrorHandler`: log (e.g. Sentry), skip user cancellations, sanitize PII; `getUserMessage` for UI.
- **PROHIBITED:** `SnackBar` for errors. Use state `hasError`/`errorMessage` or dialogs; user-facing text via `AppLocalizations`.

## 6. Logging — Apply: `flutter-logging`

- **PROHIBITED:** `print()` or `debugPrint()`. Use project `Logger` only.
- Levels: `debug`, `info`, `warning`, `error`; include context; log Notifier transitions.
- PII sanitization in logs; use `Logger.sanitizeForLogging()` when needed.

## 7. Internationalization (i18n) — Apply: `flutter-i18n`

- **PROHIBITED:** Hardcoded user-facing strings (buttons, errors, placeholders, tooltips). Use `AppLocalizations`.
- Search for `Text('...')`, `'...'`, `"..."` in modified files; add ARB keys, run `flutter gen-l10n`, replace with `AppLocalizations.of(context)!.key`.

## 8. Theme & Colors — Apply: `flutter-theme-colors`

- Use `Theme.of(context).colorScheme` and `textTheme`. **PROHIBITED:** `Color(0x...)`, `Colors.xxx` for normal UI (exceptions: `Colors.transparent`, `Colors.black`/`Colors.white` only for media overlays, with comment).
- Verify in light and dark themes.

## 9. Performance & Layout — Apply: `flutter-performance-layout`

- **PROHIBITED:** UI helper methods (e.g. `_buildHeader()`). Use `StatelessWidget` with `const`.
- Flatten nesting; `Expanded`/`Flexible` in `Row`/`Column` to avoid overflow; `Visibility` or `if` instead of `Opacity(0.0)`.
- Scroll: `CustomScrollView` + slivers for mixed content; **PROHIBITED:** nested `ListView`, `Column` in `SingleChildScrollView` for long lists, `shrinkWrap: true` in long lists.
- Keys: `ValueKey(uniqueId)` in dynamic lists; no `ValueKey(index)` unless static; no `UniqueKey()` in `build` of list items.
- Lazy: `ListView.builder`/`SliverList` for large data; offload heavy work via `compute()`.
- `if (mounted)` before `setState` in async; `if (!mounted) return` after `await`; **PROHIBITED:** `async` in `initState` — use `addPostFrameCallback`.

## 10. Resource Management — Apply: `flutter-resource-management`

- Every `AnimationController`, `ScrollController`, `TextEditingController`, `StreamSubscription` closed in `dispose()`.
- Nullification: `Controller?`; set to `null` on release; check before use.
- Guard: validate controller (e.g. `isInitialized`, `!hasError`) before use.
- Listener: store reference, `removeListener` before `dispose`.
- **PROHIBITED:** `async` in `initState`. Use `addPostFrameCallback` for post-build init.

## 11. Final Gate

- Run `flutter analyze` again. Optionally `flutter test` (or project test command) if the user wants full validation.

---

## Combined Check Checklist (Flutter/Dart)

```
Base cleanup (any language):
- [ ] Comments: remove non-needed comments (all code: Dart, Bash, Python, Java, Kotlin, Swift, TS/JS); keep docblocks, legal, complex-algorithm, config
- [ ] Unused imports, types, functions, variables, parameters, files removed (NOT reorganized/formatted)
- [ ] Empty dirs: find and remove from filesystem; remove from Git if tracked (git rm -r --cached; re-check after file removals)
- [ ] Linter/analyzer issues fixed per language (NOT code formatting)
- [ ] Config/env cleaned (AppConfig, wrangler, etc.)
- [ ] Duplicates merged into shared utils
- [ ] NO code formatting applied (dart format/flutter format etc. prohibited)

Combined quality (apply each skill):
- [ ] flutter-code-quality: flutter analyze zero issues, deps ok
- [ ] flutter-dart-standards: no dynamic, naming, const, composition
- [ ] flutter-clean-architecture: feature-based layout, no outer deps in core
- [ ] flutter-riverpod-state: NotifierProvider/state shape, Result<T> in async
- [ ] flutter-error-handling: Result, ErrorHandler, no SnackBar for errors
- [ ] flutter-logging: Logger only, no print, PII sanitization
- [ ] flutter-i18n: no hardcoded user strings, AppLocalizations
- [ ] flutter-theme-colors: colorScheme/textTheme, no hardcoded colors
- [ ] flutter-performance-layout: StatelessWidget vs helpers, scroll, keys, mounted, compute
- [ ] flutter-resource-management: dispose, nullification, guard, listener cleanup

Final:
- [ ] flutter analyze (and optionally flutter test)
```

## Order of Application

For efficiency, combine when possible:

1. **Structure & standards** (2–3): architecture and Dart rules while editing.
2. **State & errors** (4–5): Riverpod and Result/ErrorHandler in the same pass.
3. **Observability & UX** (6–8): logging, i18n, theme in UI files.
4. **Performance & resources** (9–10): layout, keys, dispose, mounted in widgets and stateful code.
5. **Analyzer & deps** (1, 11): `flutter analyze` and `pubspec` at start and end.

This keeps related edits together and reduces back-and-forth.
