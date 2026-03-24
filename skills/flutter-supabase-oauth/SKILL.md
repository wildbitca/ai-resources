---
name: flutter-supabase-oauth
description: Google Sign-In OIDC flow with Supabase Auth. Use when implementing OAuth (Google, Apple) in Flutter + Supabase apps.
triggers: "**/*.dart, oauth, google sign in, apple sign in, OIDC, supabase auth, social login, sign in with google, authentication flow, ID token"
---

# Flutter Supabase OAuth

## Google Sign-In (OIDC)

- **REQUIRED:** Implement Google Sign-In using OpenID Connect (OIDC) protocol.
- **REQUIRED:** Use `google_sign_in` package for Google authentication.
- **REQUIRED:** Server Client ID must be configured in `AppConfig` with fallback.
- **FLOW:**
    1. User taps "Sign in with Google"
    2. Flutter client requests ID Token via `google_sign_in`
    3. Supabase validates ID Token and creates/updates user profile
    4. Backend issues Supabase JWT with `user_id` and roles
- **REQUIRED:** Handle authentication errors gracefully with user-friendly messages.
- **REQUIRED:** All authentication errors must be translated using `AppLocalizations`.

## Google Cloud Console Setup

- **REQUIRED:** Enable APIs: Cloud Resource Manager, IAM, Identity Toolkit, Service Usage.
- **NOTE:** OAuth clients are managed in Google Cloud Console, not via a standalone OAuth2 API.
- **REQUIRED:** Configure OAuth 2.0 Client IDs: Web (for Supabase), Android Debug, Android Release, iOS as needed.
