# Feature: {Name}

> Status: stub | draft | reviewed | approved
> ClickUp: {link or ID — required if exists}
> Last updated: YYYY-MM-DD
> Sources: .cursorrules, codebase, ClickUp

## Problem Statement

(What user problem does this feature solve? Who benefits?)

## Requirements

### Functional

- **REQ-{FEAT}-001**: (user-facing capability — verb + object + condition)

### Non-Functional

- **NFR-{FEAT}-001**: (performance, scalability, accessibility, i18n)

### Business Rules

- **RULE-{FEAT}-001**: (invariant the system must enforce)

### Constraints

- **CON-{FEAT}-001**: (PROHIBITED items, no-go zones, platform limitations)

## Data Model

| Entity | Field | Type | Constraints | RLS |
|--------|-------|------|-------------|-----|
| | | | | |

## Architecture

- Components and their relationships
- Relevant ADRs: (link to specs/architecture/ADR-NNNN.md)
- Sequence diagrams (Mermaid) for key flows

## API Contracts

| Endpoint / Function | Method | Input | Output | Auth |
|---------------------|--------|-------|--------|------|
| | | | | |

## Test Scenarios (BDD)

### Format

Each scenario traces to a requirement. Use Gherkin syntax.

- **ID**: TS-{FEAT}-{NNN}
- **Traces**: REQ-{FEAT}-{NNN} or RULE-{FEAT}-{NNN} (which requirement this validates)
- **Priority**: critical | high | medium | low
- **Type**: unit | widget | integration | e2e
- **Automation**: automated | manual | pending

### Scenarios

#### TS-{FEAT}-001 (traces: RULE-{FEAT}-001) [critical] [unit] [automated]

```gherkin
Feature: {feature name}
  Scenario: {what is being tested}
    Given {initial state}
    When {action}
    Then {expected outcome}
```

#### TS-{FEAT}-002 (traces: REQ-{FEAT}-001) [high] [integration] [pending]

```gherkin
Feature: {feature name}
  Scenario: {what is being tested}
    Given {initial state}
    When {action}
    Then {expected outcome}
```

### Edge Cases

#### TS-{FEAT}-E01 (traces: RULE-{FEAT}-001) [medium] [unit] [pending]

```gherkin
Feature: {feature name}
  Scenario: {boundary condition}
    Given {edge case setup}
    When {action}
    Then {expected behavior at boundary}
```

## Traceability Matrix

| REQ/RULE | Scenario | Test file | Status |
|----------|----------|-----------|--------|
| RULE-{FEAT}-001 | TS-{FEAT}-001 | (path to test file) | pending |
| REQ-{FEAT}-001 | TS-{FEAT}-002 | (path to test file) | pending |

**Coverage**: {N}/{M} requirements have scenarios. {X}/{Y} scenarios have automated tests.

## Sources

| Source | What was extracted | Date |
|--------|--------------------|------|
| .cursorrules | (section/line references) | |
| Codebase | (file:line references to implicit rules) | |
| ClickUp | (task/doc IDs with descriptions) | |

## Related

- ADRs: (links)
- Cross-cutting: (links)
- ClickUp epic/tasks: (links)
