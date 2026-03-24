---
name: flutter-environment-config
description: Enforces flavor support, env files, AppConfig singleton, and no hardcoded URLs/keys in Flutter. Use when configuring environments, flavors, or build variants.
triggers: "**/*.dart, **/*.env, **/AppConfig*, flavor, environment, build variant, dev env, prd env, hardcoded URL, API key config, flutter flavor"
---

# Flutter Environment Configuration

## Flavor Support

- **REQUIRED:** Support multiple flavors (e.g. `dev`, `prd`).
- **REQUIRED:** Use environment configuration files (e.g. `config/dev.env`, `config/prd.env`).
- **REQUIRED:** Use `AppConfig` or equivalent singleton for accessing environment variables.
- **REQUIRED:** Config must use static constants or loaded values; no hardcoded URLs or API keys in source.
- **REQUIRED:** Deployments (migrations, functions, assets) via CI/CD or separate scripts—no apply-env script.

## Configuration Pattern

- **REQUIRED:** All environment-specific values must be in config files.
- **REQUIRED:** Never hardcode API URLs, keys, or secrets in source code.
- **REQUIRED:** Config must be loaded at app startup.
- **BENEFIT:** Single source of truth, easy environment switching, secure configuration management.
