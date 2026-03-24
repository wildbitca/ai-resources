---
name: flutter-ios-distribution-launch
description: Prevents and fixes iOS distribution launch failures (sysctl sandbox denials, Scene creation failed). Use when changing iOS entitlements, AppDelegate, Info.plist scene config, or debugging app failing to launch on Firebase App Distribution or TestFlight.
triggers: "**/*.plist, **/*.entitlements, **/AppDelegate*, iOS launch, distribution crash, TestFlight crash, sandbox denial, scene creation failed, firebase app distribution, app not launching"
---

# Flutter iOS Distribution Launch

Prevents and fixes iOS app launch failures on **distribution builds** (Firebase App Distribution, TestFlight) caused by sysctl sandbox denials and Scene creation failure. Shared across projects via `.cursor/skills/`.

## When to apply

- Changing **iOS entitlements** (Runner, Runner-dev, Runner-prd).
- Changing **AppDelegate** (Swift/ObjC) or **Info.plist** (scene manifest, storyboard).
- Adding or updating **Firebase**, **Sentry**, or **Google Mobile Ads** on iOS.
- User reports **app not launching** or **immediate crash** after installing from Firebase App Distribution or TestFlight.
- Device logs show **Sandbox: Runner deny sysctl-read** or **Scene creation failed**.

## Root cause (what we fixed)

1. **Sysctl denials**: Dependencies (Firebase/Crashlytics, Sentry) or the Flutter engine read `kern.bootargs` and `machdep.cpu.brand_string`. iOS sandbox denies these; with **hardened-process** entitlements the denial can be treated as fatal → process killed, "Scene creation failed".
2. **UIScene**: Plugin registration in `application(_:didFinishLaunchingWithOptions:)` instead of `didInitializeImplicitFlutterEngine(_:)` can contribute to wrong init order and scene failure.

## Fix applied (do not revert without testing)

1. **UIScene lifecycle**
    - `ios/Runner/Info.plist`: `UIApplicationSceneManifest` with `FlutterSceneDelegate`, storyboard `Main`.
    - `ios/Runner/AppDelegate.swift`: Conform to `FlutterImplicitEngineDelegate`; do **all** plugin registration and native ad factory setup in `didInitializeImplicitFlutterEngine(_:)`, not in `didFinishLaunchingWithOptions`.

2. **Hardened-process entitlements removed**
    - In `Runner.entitlements`, `Runner-dev.entitlements`, `Runner-prd.entitlements`: **do not add** `com.apple.security.hardened-process` or related keys (`checked-allocations`, `dyld-ro`, `enhanced-security-version`, `hardened-heap`, `platform-restrictions`) unless:
        - You have tested a distribution build on a real device and confirmed no "Scene creation failed" or sysctl deny in logs, or
        - Firebase/Sentry release notes confirm they handle sysctl EPERM on iOS.

## Prevention checklist

When editing iOS native config:

- [ ] No new `com.apple.security.hardened-process*` keys in any Runner entitlements without distribution-test and log check.
- [ ] Plugin registration and platform views remain in `didInitializeImplicitFlutterEngine(_:)` only.
- [ ] Info.plist keeps `UIApplicationSceneManifest` and `FlutterSceneDelegate` for the main scene.

## If launch still fails after these fixes

1. Capture device logs: `log stream --device --predicate 'process == "Runner"'` (or Console.app, filter Runner), then launch the app.
2. Search logs for: `sysctl-read`, `Scene creation failed`, `guard(23)`.
3. Update **Firebase iOS SDK** and **Sentry Flutter/iOS** to latest; check release notes for sysctl/sandbox handling.
4. If you must re-add hardened-process (e.g. App Store requirement), do so only after confirming dependencies handle sysctl denial; then re-test distribution build and logs.

## Reference

- **Project rules**: Some projects have a thin `.cursor/rules/flutter-ios-distribution.mdc` that summarizes this skill; use this skill for full detail.
- **Analysis** (if project has it): `docs/DEVICE_LOG_CRASH_ANALYSIS.md` for timeline, root cause, and applied mitigations.
