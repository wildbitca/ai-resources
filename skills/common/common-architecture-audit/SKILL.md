---
name: common-architecture-audit
description: "Protocol for auditing structural debt, logic leakage, and fragmentation across Web, Mobile, and Backend. (triggers: package.json, pubspec.yaml, go.mod, pom.xml, nest-cli.json, architecture audit, code review, tech debt, logic leakage, refactor)"
---

# Architecture Audit

## **Priority: P1 (STANDARD)**

## 📋 Audit Protocol

### 0. SOLID Compliance Check (Universal — Run First)

Before structural analysis, verify SOLID adherence across the codebase:

- **SRP**: Any class/service with 2+ unrelated responsibilities? Flag as "God Class".
- **OCP**: Are features added by modifying existing classes instead of extending via abstractions? Flag as "OCP violation".
- **LSP**: Do subclasses/implementations break the contract of their base? Flag as "LSP violation".
- **ISP**: Are there fat interfaces forcing implementors to provide unused methods? Flag as "ISP violation".
- **DIP**: Do high-level modules import concrete infrastructure (DB drivers, HTTP clients) directly? Flag as "DIP violation".

**Scoring**: -20 per SOLID violation in core/domain layer; -10 per violation in infrastructure/presentation.

### 1. Structural Duplication Discovery (Universal)

Identify split sources of truth by searching for redundant directory patterns or naming conventions.

- **Services**: Compare `Service.ts` vs `ServiceNew.ts` vs `ServiceV2.ts`.
- **Versioning**: Check for `/v1`, `/v2` or "Refactor" folders.
- **Action**:
  ```bash
  # Identify potential duplicates or legacy files
  find . -type f -name "*New.*" | sed 's/New//'
  ```

### 2. Logic Leakage Analysis (Ecosystem Specific)

Detect business logic trapped in the wrong layer (e.g., UI layer in apps, Controller layer in APIs).

#### 🌐 Web (React/Next.js/Vue)

- **Action**: `grep -rE "useEffect|useState|useMemo" components --include="*.tsx" | wc -l`
- **Threshold**: If `components/` hook count > 20x `hooks/` folder, architecture is **Monolithic**.

#### 📱 Mobile (Flutter/React Native)

- **Action**: `grep -rE "http\.|dio\.|socket\." lib/widgets --include="*.dart" | wc -l`
- **Threshold**: I/O or state mutation > 5 lines in `build()` is 🟠 High Debt.

#### ⚙️ Backend (NestJS/Go/Spring)

- **Action**: `grep -rE "Repository\.|Query\.|db\." src/controllers --include="*.ts" | wc -l`
- **Threshold**: Controllers must only handle request parsing and response formatting.

### 3. Monolith Detection (Ecosystem Specific)

Identify massive files violating Single Responsibility Principle.

- **Thresholds**:
  - **UI**: > 500 lines (🟡 Medium), > 1,000 lines (🔴 Critical).
  - **Backend Services**: > 1,500 lines indicate "God Class".
- **Action**:
  ```bash
  find . -type f \( -name "*.tsx" -o -name "*.dart" -o -name "*.go" -o -name "*.java" \) | xargs wc -l | awk '$1 > 1000'
  ```

### 4. Resource Performance Audit (Universal)

Check for large metadata or constants impacting IDE performance and binary size.

- **Threshold**: Resources > 1,000 lines require granulation.
- **Action**:
  ```bash
  find . -type f \( -name "*constants*" -o -name "*.graphql" -o -name "*strings*" \) | xargs wc -l | awk '$1 > 1000'
  ```

### 5. Quality Attributes Assessment (Universal)

Evaluate the codebase against architectural quality drivers:

| Attribute | What to Check | Red Flag |
|-----------|--------------|----------|
| **Maintainability** | Average file size, cyclomatic complexity | Files > 600 lines; functions > 30 lines |
| **Reusability** | Shared abstractions, interface usage | Copy-pasted logic across features; no shared contracts |
| **Testability** | DI usage, mock-friendly interfaces | Concrete dependencies in constructors; static calls to infra |
| **Scalability** | Stateless services, async boundaries | Global mutable state; synchronous blocking in hot paths |
| **Extensibility** | Plugin points, strategy/factory usage | Switch/if-else chains for type dispatch instead of polymorphism |

### 6. Design Pattern Usage Audit (Universal)

Verify appropriate pattern application:

- **Repository Pattern**: Data access behind interfaces? Or raw queries in services/controllers?
- **Strategy/Factory**: Behavioral variation via polymorphism? Or switch/if-else chains?
- **Observer/Events**: Cross-cutting notifications via events? Or tight bidirectional coupling?
- **Adapter/Facade**: Third-party SDKs wrapped? Or scattered direct usage across layers?

### 7. Automated Quality Metrics (Tool-Assisted)

Use static analysis tools to quantify architectural health:

| Ecosystem | Tool | Metric | Command |
|---|---|---|---|
| **Dart/Flutter** | DCM | Cyclomatic complexity, maintainability index, lines of code | `dcm analyze lib --reporter=json` |
| **Dart/Flutter** | DCM | Unused code / unused files detection | `dcm check-unused-code lib` / `dcm check-unused-files lib` |
| **TypeScript** | ESLint + `complexity` rule | Cyclomatic complexity per function | ESLint `complexity` rule (max 15) |
| **TypeScript** | Biome | Lint + complexity analysis | `npx biome check . --reporter=json` |
| **All** | SonarQube | Cognitive complexity, tech debt ratio, code smells | `sonar-scanner` with quality gate |
| **All** | Semgrep | Detect architectural anti-patterns | Custom rules for layer violations |

**Dart/Flutter quick audit:**

```bash
dcm analyze lib --fatal-style --fatal-performance --fatal-warnings --reporter=json
dcm check-unused-code lib --no-exclude-overridden
dcm check-unused-files lib
```

**TypeScript quick audit:**

```bash
npx tsc --noEmit 2>&1 | wc -l          # Type errors count
npx eslint . --format json | jq '.[] | .errorCount' | paste -sd+ | bc  # Total errors
```

## ⚖️ Scoring Impact

- **SOLID Violation (core)**: -20 per violation in domain/core layer.
- **SOLID Violation (other)**: -10 per violation in infrastructure/presentation.
- **Layer Violation**: -15 per business logic instance in UI/Controller layer.
- **Structural Fragmentation**: -10 per duplicated legacy entity.
- **Monoliths**: -10 per unit > 1,000 lines.
- **Missing Quality Attribute**: -5 per uncovered attribute (no testability strategy, no reusability patterns, etc.).
- **Missing CI Gate**: -15 if no automated static analysis or SAST in CI pipeline.

## 📚 Reference Links

- [Architecture Patterns & Remediation Protocols](references/PATTERNS.md)

> Cross-reference: See **common/best-practices** for SOLID code-level enforcement, **common/system-design** for architectural patterns, and **code-cleanup/references/TOOLING-MATRIX.md** for the full tooling matrix.


## 🚫 Anti-Patterns

- Do NOT use standard patterns if specific project rules exist.
- Do NOT ignore error handling or edge cases.
- Do NOT approve architecture without confirming static analysis tools are green.
