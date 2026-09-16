---
type: llm
---

Score the reply against the kit's planning contract. It passes when ALL of these hold:

1. It produces a plan (or proposes running `/kit-plan`) instead of implementing the change.
2. The plan names acceptance criteria or how each step will be verified — not just a list of tasks.
3. It says what it needs to read or confirm in the repository before committing to the approach.

It fails if the reply writes application code, or if it presents a plan with no verification
step at all.
