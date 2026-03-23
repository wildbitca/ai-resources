# Tooling Matrix — Static Analysis, Security, and Formatting

Complete reference for the most powerful tools per ecosystem. Each section includes install, CLI usage, and CI integration notes.

---

## 1. Dart / Flutter

### Static Analysis

| Tool              | Purpose                                | Install                        | Command                                                                        |
|-------------------|----------------------------------------|--------------------------------|--------------------------------------------------------------------------------|
| `flutter analyze` | Built-in analyzer                      | Bundled                        | `flutter analyze --fatal-infos --fatal-warnings`                               |
| **DCM**           | 475+ rules, metrics, unused code/files | `dart pub global activate dcm` | `dcm analyze lib` / `dcm check-unused-code lib` / `dcm check-unused-files lib` |

**DCM key commands:**

```bash
dcm analyze lib --fatal-style --fatal-performance --fatal-warnings
dcm check-unused-code lib --no-exclude-overridden
dcm check-unused-files lib
dcm check-dependencies lib
```

### Formatting

```bash
dart format . --line-length 80 --set-exit-if-changed
```

### Security

| Tool                          | Purpose                    | Command                                 |
|-------------------------------|----------------------------|-----------------------------------------|
| **Trivy**                     | CVE scan on `pubspec.lock` | `trivy fs --scanners vuln pubspec.lock` |
| **SonarQube**                 | SAST for Dart 3.x          | CI: `sonar-scanner` with Dart plugin    |
| **Flutter Security Analyser** | OWASP MASVS 46+ rules      | VS Code extension                       |

### Dependency Audit

```bash
dart pub outdated --json
flutter pub deps --no-dev
trivy fs --scanners vuln .
```

---

## 2. TypeScript / JavaScript (Workers, Angular, Node)

### Static Analysis

| Tool          | Purpose                                  | Install                                                                      | Command             |
|---------------|------------------------------------------|------------------------------------------------------------------------------|---------------------|
| **ESLint v9** | Lint (700+ rules, 4000+ plugins)         | `npm i -D eslint @typescript-eslint/parser @typescript-eslint/eslint-plugin` | `npx eslint .`      |
| **Biome**     | Lint + format all-in-one (10-20x faster) | `npm i -D @biomejs/biome`                                                    | `npx biome check .` |
| `tsc`         | Type-check                               | Bundled with TS                                                              | `npx tsc --noEmit`  |

**Recommended ESLint strict config (`eslint.config.js`):**

```js
import tseslint from '@typescript-eslint/eslint-plugin';
// Enable: strict + type-checked + stylistic-type-checked
// Must-have rules:
// @typescript-eslint/no-floating-promises
// @typescript-eslint/no-misused-promises
// @typescript-eslint/strict-boolean-expressions
```

**Biome alternative (faster, fewer rules):**

```bash
npx biome init
npx biome check --write .
```

### Angular-Specific

```bash
ng add angular-eslint
ng lint
```

Key `@angular-eslint` rules: `contextual-lifecycle`, `no-async-lifecycle-method`, `computed-must-return`, `prefer-standalone`.

### Formatting

| Tool         | Command                      |
|--------------|------------------------------|
| **Biome**    | `npx biome format --write .` |
| **Prettier** | `npx prettier --write .`     |

### Security / SAST

| Tool                                      | Purpose                          | Command                           |
|-------------------------------------------|----------------------------------|-----------------------------------|
| **Semgrep**                               | Pattern SAST (TS rules built-in) | `semgrep scan --config auto .`    |
| **eslint-plugin-security**                | 13 Node.js security rules        | `npm i -D eslint-plugin-security` |
| `@typescript-eslint/no-floating-promises` | Unhandled rejections             | ESLint rule                       |

### Dependency Audit

```bash
npm audit --audit-level=high
trivy fs --scanners vuln package-lock.json
```

---

## 3. Cloudflare Workers (Hono + TypeScript)

All TypeScript tools above apply, plus:

```bash
wrangler types --check      # Validate generated CF types
npx tsc --noEmit            # Type-check worker code
semgrep scan --config auto workers/
```

---

## 4. Bash / Shell

| Tool           | Purpose                     | Install                   | Command                   |
|----------------|-----------------------------|---------------------------|---------------------------|
| **ShellCheck** | Static analysis (SC* rules) | `brew install shellcheck` | `shellcheck scripts/*.sh` |
| **shfmt**      | Formatting                  | `brew install shfmt`      | `shfmt -w -i 2 scripts/`  |

---

## 5. Dockerfile

| Tool         | Purpose                        | Install                              | Command                   |
|--------------|--------------------------------|--------------------------------------|---------------------------|
| **Hadolint** | Lint Dockerfile best practices | `brew install hadolint`              | `hadolint Dockerfile`     |
| **Trivy**    | Image CVE + misconfig scan     | `brew install trivy`                 | `trivy image <image:tag>` |
| **Dockle**   | CIS benchmark for images       | `brew install goodwithtech/r/dockle` | `dockle <image:tag>`      |

---

## 6. Cross-Language (Apply to Every Project)

### Secret Detection

| Tool           | Secrets Detected                | Command                                |
|----------------|---------------------------------|----------------------------------------|
| **Gitleaks**   | 150+ types (AWS, GH, Slack, DB) | `gitleaks detect --source . --verbose` |
| **TruffleHog** | Active credential verification  | `trufflehog filesystem . --json`       |

**Pre-commit hook (Gitleaks):**

```bash
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.21.2
    hooks:
      - id: gitleaks
```

### Comprehensive Scanner

```bash
trivy fs . --scanners vuln,misconfig,secret --severity HIGH,CRITICAL
```

### SAST (Multi-Language)

```bash
semgrep scan --config auto --config p/owasp-top-ten --config p/security-audit .
```

---

## 7. CI Pipeline Template (All Ecosystems)

```yaml
# Minimal quality gate — add to any CI
steps:
  # Secret scan (fastest, run first)
  - name: Gitleaks
    run: gitleaks detect --source . --verbose --redact

  # Language-specific lint
  - name: Dart Analyze
    run: flutter analyze --fatal-infos --fatal-warnings
  - name: DCM
    run: dcm analyze lib --fatal-style --fatal-performance
  - name: TS Type Check
    run: npx tsc --noEmit
  - name: ESLint / Biome
    run: npx eslint . || npx biome check .
  - name: Hadolint
    run: hadolint Dockerfile

  # Security
  - name: Trivy FS
    run: trivy fs . --scanners vuln,misconfig,secret --severity HIGH,CRITICAL --exit-code 1
  - name: Semgrep
    run: semgrep scan --config auto --error .

  # Formatting (check only)
  - name: Dart Format Check
    run: dart format . -l 80 --set-exit-if-changed
  - name: Biome Format Check
    run: npx biome format --check . || npx prettier --check .
```

---

## Tool Selection Decision Tree

```
Is it Dart/Flutter?
├─ YES → flutter analyze + DCM + Trivy + dart format
└─ NO → Is it TypeScript?
   ├─ YES → Is it a new project?
   │  ├─ YES → Biome (lint+format) + Semgrep + Trivy
   │  └─ NO → ESLint v9 + Prettier/Biome + Semgrep + Trivy
   └─ NO → Use language table above

Always add: Gitleaks (secrets) + Trivy (CVE) + Semgrep (SAST)
```
