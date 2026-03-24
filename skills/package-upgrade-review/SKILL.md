---
name: package-upgrade-review
description: Reviews every package/dependency in the project, finds latest versions (pub.dev for Dart/Flutter, Maven for Java/Kotlin, CocoaPods/SPM for iOS), upgrades config files, and aligns code with changelogs and docs for breaking changes and new APIs. Use when the user asks to upgrade dependencies, update packages, or keep the project on latest versions.
triggers: "**/pubspec.yaml, **/build.gradle*, **/Podfile, upgrade dependencies, update packages, latest versions, breaking changes, migration guide, pub.dev, maven, cocoapods"
---

# Package Upgrade Review

## When to Use

Apply this skill when the user asks to:

- Review and upgrade all project dependencies
- Update packages to their latest versions
- Keep Dart/Flutter, Android (Java/Kotlin), or iOS dependencies current
- Align code with changelogs and migration guides after upgrades

## Prerequisites

- Identify which ecosystems the project uses: Dart/Flutter (`pubspec.yaml`), Android (Gradle), iOS (CocoaPods and/or Swift Package Manager).
- Prefer upgrading one ecosystem at a time (e.g. Flutter first, then Android, then iOS) to isolate breakage.

---

## 1. Dart / Flutter Packages

### Where dependencies live

- **File:** `pubspec.yaml` (project root).
- **Sections:** `dependencies`, `dev_dependencies`, `dependency_overrides` (use overrides sparingly).

### Finding latest versions

- **Source:** [pub.dev](https://pub.dev). Search for each package name.
- **Method:** For each package in `pubspec.yaml`, open `https://pub.dev/packages/<package_name>` and read the **Latest version** (and optionally **Changelog** / **Installing** tab).
- **Alternative:** Use `dart pub outdated` in the project root to list current vs latest (resolvable) versions for all dependencies.

### Upgrading

1. Update the version in `pubspec.yaml` (e.g. `package: ^1.2.0` → `package: ^2.0.0` or exact `2.0.0` if the project pins).
2. Run `flutter pub get` (or `dart pub get`).
3. Resolve any dependency conflicts; adjust other packages or use `dependency_overrides` only if necessary and documented.

### After upgrading: docs and changelog

- On pub.dev, open the package’s **Changelog** tab (or link from the package page).
- Read every entry **from the previously used version up to the new version**.
- Note **breaking changes**, deprecations, and new required parameters/APIs.
- In the project codebase, search for usages of that package (imports, class names, APIs).
- Apply migrations: rename APIs, fix deprecated usages, add required parameters, update types. Run `flutter analyze` and fix any new errors.

### Checklist (per Dart/Flutter package)

```
- [ ] Look up latest version on pub.dev (or run dart pub outdated)
- [ ] Update version in pubspec.yaml
- [ ] Run flutter pub get
- [ ] Read changelog from current → new version
- [ ] Find breaking/deprecation/migration notes
- [ ] Search code for package usages and update code
- [ ] Run flutter analyze (and tests if applicable)
```

---

## 2. Java / Kotlin (Android / JVM)

### Where dependencies live

- **Files:** `android/build.gradle`, `android/app/build.gradle`, `android/build.gradle.kts`, `android/settings.gradle` (or `.kts`). Sometimes root `build.gradle` for plugins.
- **Format:** Maven coordinates `group:name:version` or Gradle version catalogs.

### Finding latest versions

- **Maven Central:** [search.maven.org](https://search.maven.org) — search by `groupId` and `artifactId`.
- **Google (Android):** [developer.android.com](https://developer.android.com) build release notes, or Maven Google repo for `com.android.*`, `androidx.*`.
- **Gradle Plugin Portal:** [plugins.gradle.org](https://plugins.gradle.org) for Gradle plugins.

### Upgrading

1. For each dependency/plugin, look up the latest version on Maven Central or the official source (e.g. AndroidX, Kotlin).
2. Update the version in the appropriate `build.gradle` or `build.gradle.kts` (e.g. `implementation 'groupId:artifactId:version'` or `version = "..."` in a catalog).
3. Sync/rebuild: `cd android && ./gradlew clean` and then build the app (e.g. `flutter build apk` or run from IDE).
4. Fix any compilation or runtime errors.

### After upgrading: docs and changelog

- Check the library’s **release notes**, **changelog**, or **migration guide** (e.g. AndroidX, Kotlin, AGP).
- Apply API renames, dependency replacements (e.g. old support → AndroidX), and Gradle/AGP changes.
- Update Kotlin/Java code and Gradle scripts as needed; re-run build and tests.

### Checklist (per Java/Kotlin dependency)

```
- [ ] Find latest version (Maven Central / Google / plugin portal)
- [ ] Update version in build.gradle or build.gradle.kts
- [ ] Sync/build (e.g. ./gradlew clean build)
- [ ] Read release notes/changelog between old and new version
- [ ] Update code and Gradle config for breaking changes
- [ ] Re-run build and fix errors
```

---

## 3. iOS (CocoaPods / Swift Package Manager)

### Where dependencies live

- **CocoaPods:** `ios/Podfile` (and optionally `ios/Podfile.lock`). Versions in `pod 'Name', '~> x.y.z'` or similar.
- **Swift Package Manager:** In Xcode: File → Add Package Dependencies; or in `*.xcodeproj`/`*.xcworkspace` package references. Some projects use `Package.swift`.

### Finding latest versions

- **CocoaPods:** [cocoapods.org](https://cocoapods.org) — search for the pod name; check the **Version** and **GitHub** / **Source** for tags.
- **Swift packages:** Often on GitHub; latest release tag or main branch. Xcode shows available versions when adding/updating a package.

### Upgrading

**CocoaPods:**

1. For each pod in `Podfile`, look up the latest version on cocoapods.org (or the pod’s repo).
2. Update the version in `Podfile` (e.g. `pod 'Alamofire', '~> 5.8'` → `'~> 6.0'`).
3. Run `cd ios && pod install` (or `pod update <PodName>` for specific pod).
4. Resolve any resolution errors; fix deployment target or other pod options if required.

**Swift Package Manager:**

1. In Xcode: File → Packages → Update to Latest Package Versions (or update per package).
2. Or edit package references to point to the new version/branch/tag and resolve.

### After upgrading: docs and changelog

- Check the pod/package **CHANGELOG**, **Releases** on GitHub, or **Migration** guide.
- Update Swift/Obj-C code for renamed APIs, new requirements, or removed APIs.
- Build the iOS app (e.g. `flutter build ios` or Xcode) and run tests.

### Checklist (per iOS dependency)

```
- [ ] Find latest version (CocoaPods.org or package repo)
- [ ] Update Podfile or SPM reference
- [ ] Run pod install or refresh SPM
- [ ] Read changelog/release notes between versions
- [ ] Update iOS code for breaking changes
- [ ] Build and run (e.g. flutter build ios)
```

---

## 4. Review Docs and Changelog (All Ecosystems)

For **every** upgraded package (Dart, Java/Kotlin, iOS):

1. **Changelog:** Read from the **current** version to the **new** version. Prefer official changelog (pub.dev, GitHub Releases, Maven/CocoaPods description).
2. **Breaking changes:** Note renames, removals, signature changes, and required config (e.g. new Gradle options, new Podfile settings).
3. **Deprecations:** Replace deprecated APIs with the recommended replacement in the same upgrade pass when possible.
4. **New features:** Optionally adopt new APIs that simplify or improve the codebase; avoid unnecessary refactors if not needed.
5. **Our code:** Search the repo for imports and usages of the package; update call sites, tests, and config files. Run analyzer/build/tests after each package or batch.

### Order of work

- Upgrade version in config (`pubspec.yaml`, `build.gradle`, `Podfile`, SPM).
- Resolve dependency/install (pub get, Gradle sync, pod install).
- Read changelog and migration notes.
- Apply code and config changes.
- Run project checks (analyze, build, test).

---

## 5. Summary Table

| Ecosystem         | Config file(s)        | Where to find latest         | Install / sync command    |
|-------------------|-----------------------|------------------------------|---------------------------|
| **Dart/Flutter**  | `pubspec.yaml`        | pub.dev, `dart pub outdated` | `flutter pub get`         |
| **Java/Kotlin**   | `build.gradle(.kts)`  | Maven Central, Google, etc.  | `./gradlew clean build`   |
| **iOS CocoaPods** | `ios/Podfile`         | cocoapods.org                | `cd ios && pod install`   |
| **iOS SPM**       | Xcode / Package.swift | GitHub, Xcode                | Update in Xcode / resolve |

---

## 6. Full Workflow (Single Pass)

1. **List** all dependencies per ecosystem (read pubspec.yaml, build.gradle, Podfile, SPM).
2. **For each package:** find latest version → update config → run install/sync.
3. **For each upgraded package:** read changelog (current → new) → list breaking changes and deprecations → search code for usages → update code and config → run analyzer/build/tests.
4. **Repeat** until all packages are upgraded and the project builds and passes tests.

Prefer upgrading in small batches (e.g. one major package at a time) so breakages are easy to attribute. Document any intentional holdbacks (e.g. “keeping package X at 1.x until Y is migrated”).
