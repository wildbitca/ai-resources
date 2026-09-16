---
max_turns: 4
# Write and Task are allowed on purpose: this case fails if the kit fires ceremony on a plain
# question, and that can only be observed if creating a handoff or delegating is possible.
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Task]
---

What's the difference between `git fetch` and `git pull`?
