---
name: self-update
description: "Update the ai-resources kit install and re-apply it: upgrade the package, regenerate workflow skills and the skills index, refresh skill links, agents, hooks and instruction blocks. Use when the user says /self-update or asks to update the kit."
disable-model-invocation: true
---

# /self-update

These commands change the local install and the user's agent configuration — show them to the user and run them only after they agree.

```bash
brew update && brew upgrade ai-resources   # Homebrew install
# git checkout instead: git -C "$AGENT_KIT" pull --ff-only

ai-resources generate   # vendor skills, workflow-* skills, skills-index.json
ai-resources setup      # skill links, subagents, hooks, managed instruction blocks
```

`ai-resources setup` rewrites only the block between `<!-- BEGIN ai-resources … -->` and `<!-- END ai-resources -->` in instruction files such as `~/.claude/CLAUDE.md`; everything outside that block is kept. Kit hooks in `settings.json` are replaced; other hooks are kept.

## Adding or refreshing remote skill packs

1. Edit `skills.sources` in `$AGENT_KIT/resources.json`.
2. Preview with `ai-resources generate --dry-run`, then run `ai-resources generate`.
3. A conflict with an existing skill directory stops the import. Rename the skill, or rerun with `--force` only when replacing it is intended.
