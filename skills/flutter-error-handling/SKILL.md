---
name: flutter-error-handling
description: Applies Result-type error handling, central error processing, and UI error display patterns in Flutter/Dart. Use when implementing async services, Notifiers, or user-facing error messages.
---

# Flutter Error Handling

## Result Pattern (RO-RO)

- Use a "Receive Object, Return Object" pattern for async data-layer functions.
- All async service methods should return a `Result<T>` sealed type with:
  - `Success<T>` — holds the data
  - `Failure<T>` — holds an `AppError` (or project equivalent) with message, optional code, and details
- Use pattern matching (`switch` or `when`) to handle results. **PROHIBITED:** Uncontrolled `throw`; **PROHIBITED:** `try/catch` without converting to `Result<T>`.

### Example

```dart
switch (result) {
  case Success(:final data):
    state = state.copyWith(data: data, isLoading: false, hasError: false);
  case Failure(:final error):
    state = state.copyWith(isLoading: false, hasError: true, errorMessage: error.message);
}
```

- Set a loading flag before async work; clear it in both success and failure branches.

## Error Types

Define errors that extend a base `AppError` (or equivalent) with:

- `message` (String) — user-facing
- `code` (String?) — for branching (e.g. `'OTP_EXPIRED'`, `'OTP_INVALID'`)
- `details` (Map<String, dynamic>?) — extra context

Common variants: `NetworkError`, `AuthenticationError`, `CacheError`, `UploadError`, domain-specific errors. Include `statusCode` or `retryable` where useful.

## Central Error Handler

- Use a single `ErrorHandler` (or similar) for processing errors:
  - Log to your observability tool (e.g. Sentry), except for user cancellations.
  - Convert exceptions to `AppError` types.
  - Skip reporting for cancellations (e.g. `'GOOGLE_SIGN_IN_CANCELLED'`).
  - Sanitize PII from messages and details before logging.
- Use `ErrorHandler.getUserMessage(error, context)` (or equivalent) for user-facing text, wired to `AppLocalizations` when available.

## Chaining Multiple Error Handlers (Sentry + Crashlytics)

When using **both** Sentry and Firebase Crashlytics:

- Chain handlers so both receive errors. Use an initializer (e.g. `FirebaseCrashlyticsInitializer`) that:
  1. Attaches Crashlytics handlers for `FlutterError.onError` and `PlatformDispatcher.instance.onError`.
  2. Forwards to the previous handler (Sentry) after reporting to Crashlytics.
- **REQUIRED:** CI should upload symbols to Crashlytics post-build for native crash symbolication.
- **BENEFIT:** Sentry for full-stack traces; Crashlytics for native crash aggregation and symbol upload.

## Deprecated Patterns (PROHIBITED)

- **SnackBar:** Do not use for error messages or user feedback; use state-based error display.
- **Old Supabase Auth cookie methods:** Use modern JWT-based authentication only.
- **Facebook Authentication:** If OAuth app, support only Google and Apple; do not add Facebook auth.

## User Feedback

- **PROHIBITED:** Using `SnackBar` for error messages.
- **REQUIRED:** Surface errors via:
  - State: `hasError`, `errorMessage` in state; render `if (state.hasError) ErrorWidget(state.errorMessage)` (or similar).
  - Dialogs for critical errors.
- All user-visible error text should be localized (e.g. `AppLocalizations`).
