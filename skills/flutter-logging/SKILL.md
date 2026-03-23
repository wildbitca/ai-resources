---
name: flutter-logging
description: Enforces structured logging, log levels, and PII sanitization in Flutter/Dart apps. Use when adding logs, debugging, or configuring production-safe logging.
---

# Flutter Logging

## Logger Usage

- Use a project `Logger` (or equivalent) for all logging. **PROHIBITED:** `print()` or `debugPrint()`.
- Typical methods and use:

  | Level   | Use for |
      |--------|----------|
  | `debug` | Development flow, detailed traces (only in debug or when configured) |
  | `info`  | Important events, state changes |
  | `warning` | Recoverable issues, handled but unexpected situations |
  | `error` | Failures with exception and stack trace |

- Include context in messages (e.g. `"ActiveProfileNotifier: Saved active profile: ${id}"`).
- Log state transitions in Notifiers/providers (loading, success, error).

## PII and Sanitization

- Logger should sanitize PII (emails, UUIDs, tokens, bearer strings) from log messages and context.
- Define a small allowlist of non-sanitized keys if needed for debugging entity IDs. Common examples: `pet_id`, `petId`, `user_id`, `userId`, `post_id`, `postId`, `owner_id`, `ownerId`. Extend per project.
- Use `Logger.sanitizeForLogging()` (or equivalent) when passing custom structures.

## Integration

- `debug`: only in debug builds; optionally to Sentry in dev.
- `info`/`warning`/`error`: sent to your observability service (e.g. Sentry) in production as per project config.
