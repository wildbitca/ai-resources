---
name: firebase-crashlytics-fix-issues
description: Uses Firebase MCP to fetch Crashlytics top issues for a given project/app, get issue details and event stack traces, and fix them one by one in the codebase. Use when the user wants to fix Crashlytics crashes, triage production errors, or evaluate a specific Firebase project's stability.
---

# Firebase Crashlytics: Fix Issues (evaluar proyecto)

## Quick start

1. **Resolve project and app:** `firebase_get_environment`; if needed `firebase_list_projects` / `firebase_update_environment` and `firebase_list_apps` to get `appId`.
2. **List top issues:** `crashlytics_get_report(appId, report: "topIssues", pageSize: 10)`.
3. **Per issue:** `crashlytics_get_issue(appId, issueId)` → `crashlytics_list_events` / `crashlytics_batch_get_events` for stack traces → apply fix in code → run analyzer/tests → document (no MCP "resolve"; user can close in Console).

## When to Use

Apply this skill when the user asks to:

- Fix Firebase Crashlytics issues or crashes for a given project
- Evaluate or triage stability of a specific Firebase project/app
- Work through top crashes reported in Crashlytics
- Get and fix production crashes from Crashlytics (Android/iOS)

**Example prompts:** *"Fix my Crashlytics issues"*, *"Evaluate Crashlytics for this project"*, *"Go through top crashes in Firebase"*, *"Fix production crashes from Crashlytics for the app I specify"*.

## Prerequisites

- Firebase MCP must be configured and authenticated (`firebase_login` if needed).
- User may specify the **project** (Firebase project ID or project directory) and optionally the **app** (iOS/Android); otherwise use current environment and list apps to choose.

## Workflow

### 1. Resolve Firebase project and app (proyecto a evaluar)

- **Current env:** `firebase_get_environment` → `active_project`, `project_dir`, `active_user_account`.
- **If user specifies another project:**
    - By project ID: `firebase_update_environment(active_project: "<project_id>")`.
    - By project directory (workspace): `firebase_update_environment(project_dir: "<path_to_dir_with_firebase.json>")`.
- **Get app ID(s) for Crashlytics:** `firebase_list_apps(platform?)`:
    - Omit `platform` to list all apps (iOS, Android, Web).
    - Use `platform: "android"` or `platform: "ios"` to limit. Crashlytics is typically used for Android/iOS.
- **Result:** One or more `appId` (e.g. `1:123456789:android:abc-def`). Use the app the user wants to evaluate; if unspecified, use the first Android or iOS app.

### 2. List top issues (reportes)

- **Tool:** `crashlytics_get_report`
- **Parameters:**
    - `appId` (required): from step 1.
    - `report`: `"topIssues"` to get the list of issues to fix (or `"topVariants"` for variant-level view).
    - `pageSize`: e.g. 10–25.
    - `filter` (optional): e.g. `intervalStartTime` / `intervalEndTime` (ISO 8601), `issueErrorTypes` (e.g. `["FATAL","NON_FATAL"]`), `versionDisplayNames`, etc.
- **Result:** Rows with issue identifiers and counts. Extract `issueId` (and variant IDs if needed) for the next step.

### 3. For each issue: get details and stack traces

**3a. Issue data (debugging)**

- **Tool:** `crashlytics_get_issue`
- **Parameters:** `appId`, `issueId` (hex UUID from report).
- **Use from response:** summary, type (FATAL/NON_FATAL/ANR), sample event resource names, first/last occurrence, etc. Use `sampleEvent` (event resource names) for 3b.

**3b. Sample events and stack traces**

- **Tool:** `crashlytics_list_events`
    - **Parameters:** `appId`, `filter: { issueId: "<issueId>" }`, `pageSize`: e.g. 3–5.
    - **Result:** List of events with resource `names` (event resource names).
- **Tool:** `crashlytics_batch_get_events`
    - **Parameters:** `appId`, `names`: array of event resource names from 3a or 3b.
    - **Use from response:** stack traces, device/OS, version, etc. Prefer in-app frames (project `lib/`, `src/`, or app package) to locate file/line.

### 4. Apply the fix in the codebase

1. **Locate:** Use stack trace from batch_get_events to find file and line (or surrounding block). Prefer top-most in-app frame that matches the project.
2. **Change:** Implement the fix (null check, error handling, type guard, API contract, etc.). Follow project rules (e.g. Result pattern, ErrorHandler, no `print`).
3. **Verify:** Run the project’s checks (e.g. `flutter analyze`, `dart analyze`, or test command). Fix any new issues before moving on.
4. **Optional:** Run a quick smoke/test for the affected flow if the user wants.

### 5. Document and continue (no “resolve” in MCP)

- Firebase Crashlytics MCP does **not** expose “resolve” or “close issue”. After fixing in code:
    - Document which `issueId` (and appId) were addressed so the user can close or annotate them in the [Firebase Console](https://console.firebase.google.com) if desired.
- Then **repeat from step 3** for the next issue until the list is done or the user stops.

## Checklist (per issue)

```
- [ ] crashlytics_get_issue(appId, issueId)
- [ ] crashlytics_list_events(appId, filter: { issueId }) and/or crashlytics_batch_get_events(appId, names)
- [ ] Locate file/line from stack trace (in-app frames)
- [ ] Edit code and apply fix
- [ ] Run analyzer/tests
- [ ] Document fixed issue (issueId, appId) for user to close in Console
- [ ] Next issue
```

## Tool choice summary

| Goal                    | Tool                           | Key args                                                             |
|-------------------------|--------------------------------|----------------------------------------------------------------------|
| Current project / app   | `firebase_get_environment`     | —                                                                    |
| Set project to evaluate | `firebase_update_environment`  | `active_project` or `project_dir`                                    |
| List apps (get appId)   | `firebase_list_apps`           | `platform` (optional): "ios" \| "android" \| "web" \| "all"          |
| List top issues         | `crashlytics_get_report`       | `appId`, `report: "topIssues"`, `pageSize`, optional `filter`        |
| Issue details           | `crashlytics_get_issue`        | `appId`, `issueId`                                                   |
| Sample events for issue | `crashlytics_list_events`      | `appId`, `filter: { issueId }`, `pageSize`                           |
| Stack traces (events)   | `crashlytics_batch_get_events` | `appId`, `names` (event resource names from get_issue / list_events) |

## Tips

- **In-app vs system frames:** Prefer frames from the project’s `lib/`, `src/`, or app package. Ignore or down-rank SDK, runtime, and third‑party package frames when locating project code.
- **Flutter/Dart:** Look for `package:app_name/` or `package:your_project/` in the stack. Symbolication may depend on uploaded dSYM (iOS) and Proguard/mapping (Android); ensure symbols are uploaded for readable stack traces.
- **Proyecto especificado:** If the user says “evalúa el proyecto X”, set that project with `firebase_update_environment(active_project: "X")` or `firebase_update_environment(project_dir: "path/to/X")` before listing apps and issues.
- **Batching:** If there are many issues, fix 5–10, run the full test suite, then continue. Document all fixed issueIds for the user.
- **Duplicates / similar:** If several issues share the same root cause, fix once and document all related issueIds.
- **Time range:** Use `filter.intervalStartTime` and `filter.intervalEndTime` (ISO 8601, within last 90 days) to focus on recent crashes.
