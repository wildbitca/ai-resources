---
name: flutter-http-dio
description: Enforces Dio-based HTTP client patterns with factory constructors and interceptors in Flutter. Use when implementing API clients, upload services, or authenticated requests.
triggers: "**/*.dart, dio, HTTP client, API client, interceptor, upload service, authenticated request, REST API, network layer, http request"
---

# Flutter HTTP Client (Dio)

## Single HTTP Client

- Use a single `HttpClient` (or equivalent) class wrapping Dio for all backend API requests.
- **REQUIRED:** Centralize timeouts, logging, and auth handling. Avoid creating ad-hoc Dio instances in services.

## Factory Pattern

Provide factory constructors for different use cases:

| Factory               | Use Case                             | Timeouts        | Auth                  |
|-----------------------|--------------------------------------|-----------------|-----------------------|
| `create`              | Basic requests, optional base URL    | Default         | No                    |
| `createAuthenticated` | JWT-protected APIs (upload, routing) | Default         | Yes (AuthInterceptor) |
| `createQuick`         | Routing, health checks               | Short (e.g. 1s) | No                    |
| `createForUpload`     | File uploads                         | Long (e.g. 2h)  | Yes                   |

- **REQUIRED:** Use `createAuthenticated` for endpoints that require a Bearer token. Inject the auth service and add an interceptor that attaches the token.

## Auth Interceptor

- Add an `AuthInterceptor` (or equivalent) that:
    - Reads the current session/token from the auth service.
    - Adds `Authorization: Bearer <token>` to requests.
    - Handles token refresh or 401 responses per project needs.

## Logging

- In debug builds, add a Dio `LogInterceptor` (or custom interceptor) that logs request/response for troubleshooting.
- **PROHIBITED:** Logging sensitive data (tokens, passwords) in production.

## Timeouts

- Set `connectTimeout`, `receiveTimeout`, `sendTimeout` appropriate to the use case.
- Use short timeouts for routing/health checks; long timeouts for uploads.

## Project Integration

- Store base URLs and timeout constants in config (e.g. `ApiConstants`, `AppConfig`).
- Services obtain the client from a provider or factory; do not construct Dio in every service method.
