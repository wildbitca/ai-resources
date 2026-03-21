---
name: flutter-app-size
description: Reduces Flutter release APK/AAB/IPA size via R8, tree-shake-icons, asset diet, HTTP consolidation, deferred loading. Use when optimizing app size, reducing APK/AAB size, modifying build config, adding assets, or when the user mentions app size, binary size, or install size.
---

# Flutter App Size Optimization

Applies practices that keep release APK/AAB/IPA size small. Reference: [I Shrunk a Flutter Release From 68 MB to 27 MB](https://medium.com/@garoono/i-shrunk-a-flutter-release-from-68-mb-to-27-mb-with-every-feature-intact-0da852103385).

## When to apply

- User asks to reduce app size, APK size, or install size
- Modifying `android/app/build.gradle.kts`, `proguard-rules.pro`, CI build scripts
- Adding or changing assets in `pubspec.yaml`
- Adding HTTP or network dependencies
- Adding heavy features (chat, emoji picker, image editor, camera)

## Android (build.gradle.kts)

- **REQUIRED** in `buildTypes.release`: `isMinifyEnabled = true`, `isShrinkResources = true`
- **REQUIRED**: ProGuard files `proguard-android-optimize.txt` + `proguard-rules.pro`
- ProGuard rules must keep Flutter, Room, WorkManager; update app package (e.g. `dev.wildbit.pacha.**`)

## Flutter build flags (CI)

- **REQUIRED** for release: `--obfuscate`, `--split-debug-info=/tmp/symbols`, `--tree-shake-icons`
- **REQUIRED** production: `flutter build appbundle` (AAB for Play Dynamic Delivery)
- Optional dev APK: `--split-per-abi` for smaller test installs

## Assets (pubspec.yaml)

- **REQUIRED**: List explicit asset files, not folder paths (avoids bundling .DS_Store, README.md)
- Prefer WebP over PNG where transparency/quality permits
- Add each new asset file explicitly when adding assets
- Convert large PNGs: `cwebp -q 85 input.png -o output.webp`

## HTTP / Dependencies

- **REQUIRED**: Use one HTTP stack (Dio); do not add `http` package alongside Dio
- Replace `http.get`/`http.head` with Dio equivalents
- Remove zombie or duplicate plugins
- Audit: `flutter pub deps --style=compact`, `dart pub outdated`

## ProGuard rules

- Keep app package: `-keep class dev.wildbit.pacha.** { *; }`
- Keep Flutter, Room, WorkManager per Flutter/Android docs
- Do not use legacy package names (e.g. `resource_frontier` if obsolete)

## Heavy features (optional)

- Consider `deferred as` imports for rarely-used features: `stream_chat_flutter`, `emoji_picker_flutter`, `pro_image_editor`, `camerawesome`
- Load before navigation: `await module.loadLibrary();`

## Sanity checklist

- [ ] R8 minify + shrinkResources enabled
- [ ] --tree-shake-icons in Flutter build commands
- [ ] Assets listed explicitly; no folder paths
- [ ] Single HTTP stack (Dio)
- [ ] ProGuard keeps app package and Flutter/plugins

## Reference

- **Project rules**: Some projects have `.cursor/rules/flutter-app-size.mdc`; use this skill for full detail.
