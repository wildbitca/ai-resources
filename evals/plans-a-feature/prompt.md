---
max_turns: 6
# Write and Task are allowed so the grader's failure modes are reachable: a run that writes
# code instead of planning must be able to fail, not be blocked by the tool list.
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Task]
---

I want to add rate limiting to our API before we ship it. Plan the work first — I don't want code yet.
