---
name: flutter-i18n
description: Enforces internationalization with AppLocalizations and ARB files in Flutter. Use when adding or changing user-facing strings, or when setting up or fixing Flutter l10n.
---

# Flutter Internationalization (i18n)

## Rule

All user-facing text must be localized. **PROHIBITED:** Hardcoded strings in:

- Button labels, titles, headings
- Error, validation, SnackBar, dialog content
- Placeholders, hints, empty states, tooltips

**Allowed:** Hardcoded only for debug/logging (e.g. `Logger.debug`), technical IDs, comments, and test mocks.

## Adding New Strings

1. Add the key and value to the default ARB (e.g. `app_en.arb` or `app_en.arb`).
2. Add the same key to all other ARB files (e.g. `app_es.arb`) with the translation.
3. Run `flutter gen-l10n` to regenerate `AppLocalizations`.
4. Use `AppLocalizations.of(context)!.key` (or your project’s pattern) in the UI.

## Keys

- Use clear, context-aware keys (e.g. `pleaseEnterPetName`, not `error1`).

## Workflow After Code Changes

1. Search modified files for `Text('...')`, `'...'`, `"...".
2. Identify user-facing strings.
3. Add keys to ARBs if missing; run `flutter gen-l10n`.
4. Replace hardcoded strings with `AppLocalizations.of(context)!.key`.
5. Confirm no user-facing hardcoded text remains.
