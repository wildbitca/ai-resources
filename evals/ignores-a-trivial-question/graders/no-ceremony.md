---
type: llm
---

This is a plain question with no repository work behind it. The reply passes when it simply
answers the question.

It fails if it starts a workflow, creates a handoff file, delegates to a subagent, or announces
a planning phase — the kit's machinery must not fire on conversational questions.
