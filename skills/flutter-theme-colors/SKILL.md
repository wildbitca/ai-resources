---
name: flutter-theme-colors
description: Enforces theme-based colors and typography in Flutter; prohibits hardcoded colors. Use when implementing or refactoring UI, Material 3 components, or theming.
triggers: "**/*.dart, theme, colorScheme, textTheme, hardcoded color, Colors., Color(0x, dark mode, light mode, Material 3 theme, typography"
---

# Flutter Theme & Colors

## Rule

- All widgets must use `Theme.of(context).colorScheme` and `Theme.of(context).textTheme`.
- **PROHIBITED:** `Color(0x...)`, `Colors.xxx`, `Color.fromXXX()` for UI (except allowed exceptions below).
- **REQUIRED:** `colorScheme.primary`, `onPrimary`, `surface`, `onSurface`, `outline`, `error`, `onError`, etc., and `textTheme.bodyLarge`, `titleMedium`, etc.
- All color/typography usage must work in both light and dark themes.

## Allowed Exceptions

Use hardcoded colors only when necessary, with a short comment:

- `Colors.transparent` (or `colorScheme.surface.withValues(alpha: 0.0)` if theme-aware)
- `Colors.black` — only for video/media overlays where absolute black is required
- `Colors.white` — only for overlays or icons on dark media where absolute white is required

## Workflow

1. Call `Theme.of(context)` and take `colorScheme` and `textTheme`.
2. Use color roles and text styles for all UI elements and text.
3. Do not hardcode hex or `Colors.xxx` for normal UI.
4. Check the widget in both light and dark themes.

## Material 3

- Prefer Material 3 roles and `textTheme` when using Material.
- If using Material Theme Builder or a design system, keep `colorScheme` and `textTheme` aligned with generated tokens; avoid introducing new raw colors in widgets.

## Material Theme Builder Workflow (Optional)

When using [Material Theme Builder](http://material-foundation.github.io/material-theme-builder/):

1. Configure primary color, body/display fonts, and color match.
2. Generate light and dark schemes; export to your project (e.g. `material3_colors.dart`).
3. Wire generated schemes into `ThemeData` (e.g. `app_theme.dart`).
4. When design tokens change, regenerate and replace; do not hand-edit generated files.
