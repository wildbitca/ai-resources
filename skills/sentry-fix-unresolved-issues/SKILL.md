---
name: sentry-fix-unresolved-issues
description: Uses Sentry MCP to fetch unresolved issues, get details and Seer analysis, and fix them one by one in the codebase. Use when the user wants to fix Sentry issues, triage production errors, work through unresolved bugs, or clear the Sentry inbox.
triggers: "sentry, unresolved issues, production error, sentry inbox, triage errors, fix sentry, seer analysis, error monitoring, sentry issue"
---

# Sentry: Fix Unresolved Issues

## Quick start

1. **Resolve org** (and optionally project) via `find_organizations` / `find_projects`.
2. **List unresolved:** `search_issues(organizationSlug, naturalLanguageQuery: "unresolved issues", limit: 10)`.
3. **Per issue:** `get_issue_details(issueUrl)` → `analyze_issue_with_seer(issueUrl)` → apply fix in code → run analyzer/tests → `update_issue(issueUrl, status: "resolved")`.

## When to Use

Apply this skill when the user asks to:

- Fix Sentry issues, resolve production errors, or work through the Sentry inbox
- Triage or fix "unresolved" / "open" issues
- Systematically fix bugs reported in Sentry

**Example prompts:** *"Fix my Sentry issues"*, *"Go through unresolved bugs in Sentry"*, *"Clear my Sentry inbox"*, *"Fix production errors from Sentry one by one"*.

## Prerequisites

- Sentry MCP must be configured and authenticated.
- User may provide `organizationSlug` or `projectSlug`; otherwise discover them via `find_organizations` and `find_projects`.

## Workflow

### 1. Get organization and (optional) project

- If unknown: `find_organizations` (optional `query`). Use `organizationSlug` from the first/desired org.
- Optionally: `find_projects(organizationSlug, query?)` to limit to a project. Pass `projectSlugOrId` to `search_issues` when scoping.

### 2. Search for unresolved issues

- **Tool:** `search_issues`
- **Parameters:**
    - `organizationSlug` (required)
    - `naturalLanguageQuery`: e.g. `"unresolved issues"`, `"is:unresolved"`, or `"unresolved issues from the last 7 days"`
    - `projectSlugOrId` (optional): to scope to one project
    - `limit`: 10–25 to process in one run; increase if user wants more
- **Result:** List of issues with `issueId`, `title`, `permalink` (issue URL), `status`, etc. Use `permalink` or `issueUrl` for subsequent calls.

### 3. For each issue: get details and analyze

**3a. Get full details**

- **Tool:** `get_issue_details`
- **Use:** `issueUrl` = `permalink` from the search result (simplest), or `organizationSlug` + `issueId`.
- **Use from response:** `title`, `culprit`, `metadata`, `stacktrace` (file paths, line numbers, in-app vs system frames), `message`, `firstSeen`, `lastSeen`, `count`, `userCount`.

**3b. (Recommended for code fixes) Seer analysis**

- **Tool:** `analyze_issue_with_seer`
- **Use:** `issueUrl` (or `organizationSlug` + `issueId`). Optionally `instruction` for extra context (e.g. "focus on the Flutter/Dart stack").
- **Use from response:** root cause, suggested file/line, concrete code changes. Prefer in-app frames; ignore system/package frames when locating project code.

### 4. Apply the fix in the codebase

1. **Locate:** Use stack trace and Seer output to find the correct file and line (or surrounding block). Prefer top-most in-app frame that matches the project.
2. **Change:** Implement the fix (null check, error handling, type guard, API contract, etc.). Follow project rules (e.g. Result pattern, ErrorHandler, no `print`).
3. **Verify:** Run the project’s checks (e.g. `flutter analyze`, `dart analyze`, or test command). Fix any new issues before moving on.
4. **Optional:** Run a quick smoke/test for the affected flow if the user wants.

### 5. Mark as resolved and continue

- **Tool:** `update_issue`
- **Parameters:** `issueUrl` (or `issueId` + `organizationSlug`), `status: "resolved"`. Only after the fix is applied and verified.
- If the user prefers to resolve in a batch, skip this step and only fix in code; document which issues were fixed for manual resolution in Sentry.

Then **repeat from step 3** for the next issue until the list is done or the user stops.

## Checklist (per issue)

```
- [ ] get_issue_details(issueUrl)
- [ ] analyze_issue_with_seer(issueUrl) if a code fix is needed
- [ ] Locate file/line from stack trace and Seer
- [ ] Edit code and apply fix
- [ ] Run analyzer/tests
- [ ] update_issue(issueUrl, status: "resolved") when verified
- [ ] Next issue
```

## Tool choice summary

| Goal                   | Tool                      | Key args                                                      |
|------------------------|---------------------------|---------------------------------------------------------------|
| List unresolved issues | `search_issues`           | `organizationSlug`, `naturalLanguageQuery` "unresolved"       |
| Issue details          | `get_issue_details`       | `issueUrl` (from `permalink`) or `organizationSlug`+`issueId` |
| Root cause + fix ideas | `analyze_issue_with_seer` | `issueUrl` or `organizationSlug`+`issueId`                    |
| Mark fixed             | `update_issue`            | `issueUrl`, `status: "resolved"`                              |

## Tips

- **In-app vs system frames:** Prefer frames from the project’s `lib/`, `src/`, or app package. Ignore or down-rank SDK, language runtime, and third‑party package frames unless Seer clearly points there.
- **Flutter/Dart:** Look for `package:app_name/` or `package:your_project/` in the stack. Use `analyze_issue_with_seer` with an `instruction` like “Focus on Dart/Flutter and our app package” if the stack is noisy.
- **Batching:** If there are many issues, fix 5–10, run the full test suite, then continue. Ask the user if they want to resolve in Sentry after each fix or in a batch.
- **Duplicates / similar:** If several issues share the same root cause, fix once and resolve all related issues (or document them for the user to merge in Sentry).
- **regionUrl:** For EU Sentry (`eu.sentry.io`) or self-hosted, pass `regionUrl` to `search_issues`, `get_issue_details`, `analyze_issue_with_seer`, and `update_issue` when supported.
