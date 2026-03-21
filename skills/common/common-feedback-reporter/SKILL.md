---
name: common-feedback-reporter
description: "CRITICAL - Before ANY file write, audit loaded skills for violations. Log violations to knowledge/searchable/. (triggers: **/*, write, edit, create, generate, skill, violation)"
---

# Feedback Reporter

## **Priority: P0 - Auto-detect skill violations before file writes**

## Checkpoint: Before File Writes

**Quick check before `write_to_file`, `replace_file_content`, `multi_replace_file_content`:**

1. **Check** - Any skills loaded for this file extension?
   - NO → Proceed safely
   - YES → Continue to step 2
2. **Audit** - Does planned code violate loaded skill rules?
   - NO → Proceed
   - YES → Log violation, then fix before writing

## Detection Flow

```
Before file write?
├─ Check file extension → Identify loaded skills
├─ Review skill anti-patterns/rules
├─ Code matches anti-pattern?
│  ├─ YES → VIOLATION → log + fix
│  └─ NO → Proceed
└─ No skills loaded → Proceed
```

## Examples (Quick Reference)

**Flutter**: `color: Colors.blue` → Rule: No hardcoded colors → Fix: use theme

**React**: `class MyComponent extends...` → Rule: Use functions → Fix: convert

**SKILL.md**: 105 lines → Rule: ≤100 lines max → Fix: extract to references/

## Reporting Violations

When a violation is caught, append a line to `knowledge/searchable/skill-violations.log` (create if absent):

```
[YYYY-MM-DD HH:MM] skill=<skill-id> file=<path> violation="<description>" rule="<exact rule>" action="<fix applied>"
```

If the file is not writable or `knowledge/` does not exist, log the violation in the handoff file under `Unresolved / risks` instead. The goal is traceability — no silent violations.

## Pre-Completion Check

Before `notify_user` or task completion:

**Did I write code?** YES → **Did I audit skills?** NO → Audit now

## Anti-Patterns

- **No "I'll check later"**: Check before writing, not after
- **No "minor change skip"**: Every write needs check
- **No "user waiting skip"**: 10-second check > pattern violation
