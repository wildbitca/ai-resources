---
name: flutter-dependency-install
description: Adds or updates Flutter/Dart dependencies using the latest possible version and official pub.dev documentation for correct installation and configuration. Use when adding packages to pubspec.yaml, installing dependencies, or when the user asks to add or update a Flutter/Dart package.
triggers: "**/*.dart, **/pubspec.yaml, pub.dev, flutter pub add, add package, install dependency, update package, pubspec, dart dependency"
---

# Flutter Dependency Install

When adding or updating a Flutter/Dart dependency, follow this workflow so the project always uses the latest appropriate version and is configured according to official docs.

## Workflow

1. **Resolve latest version and read official docs**  
   Before editing `pubspec.yaml` or running `flutter pub add`:
    - Fetch the package page from pub.dev: `https://pub.dev/packages/<package_name>` (use the available web fetch tool).
    - Read the **Installing** section and the **README** (and **Usage** if present) to determine:
        - Recommended version or version constraints (if any).
        - Required setup steps (e.g. Android/iOS config, manifest, `Info.plist`, initialization in `main.dart`).
        - Any platform-specific or optional configuration.
    - Prefer the **latest stable** version unless the docs or project constraints require a specific range.

2. **Add dependency with latest version**
    - Prefer: `flutter pub add <package_name>` — resolves and adds the latest compatible version and runs `pub get`.
    - If the project or docs require a specific version/range: `flutter pub add <package_name>:<version>` and document why in a short comment or commit message.
    - For dev dependencies: `flutter pub add dev:<package_name>`.

3. **Apply configuration from official docs**  
   Using what you read in step 1:
    - Apply any Android configuration (e.g. `AndroidManifest.xml`, `build.gradle`, `network_security_config.xml`).
    - Apply any iOS configuration (e.g. `Info.plist`, `Podfile`).
    - Add any required initialization (e.g. in `main.dart`) or registration (e.g. generated plugins).
    - Do not skip steps marked as required in the package’s Installing/README.

4. **Verify**
    - Run `flutter pub get` if you edited `pubspec.yaml` by hand.
    - Run `flutter analyze` and fix any new issues.
    - Apply **flutter-code-quality** when considering the task complete.

## Rules

- **REQUIRED**: Read the package’s official pub.dev page (Installing + README/Usage) before adding or reconfiguring.
- **REQUIRED**: Use the latest possible version unless the docs or project require otherwise (then document the reason).
- **REQUIRED**: Perform all installation and configuration steps from the official docs.
- **PROHIBITED**: Adding a package without checking pub.dev for current install and configuration instructions.
- **PROHIBITED**: Pinning an old version without a documented reason (e.g. compatibility, security).

## Quick checklist

- [ ] Fetched `https://pub.dev/packages/<package_name>` and read Installing + README/Usage
- [ ] Added dependency with `flutter pub add <package_name>` (or dev/version with justification)
- [ ] Applied all required setup from the docs (Android/iOS/manifest/initialization)
- [ ] Ran `flutter pub get` and `flutter analyze`; zero new issues
