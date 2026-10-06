<!-- BEGIN ai-resources: managed by `ai-resources setup`; edits inside this block are overwritten -->
# ai-resources (OpenClaw agent)

Kit root: `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec`. After `brew upgrade ai-resources`, run `ai-resources setup`.

## Kit workflow

- Before any multi-step work, load the `kit-orchestration` skill: it explains how to run a workflow (steps, handoff, parallel groups, return format).
- Workflows are the `workflow-*` skills (feature, bugfix, refactor, incident response, ...). Pick one by reading each skill's description, which is its trigger. Definitions live in `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec/workflows/`.
- Work as a team with the kit roles rather than in one head: `planner`, `software-architect`, `implementer`, `tester`, `code-reviewer`, `security-auditor`, `verifier`, `doc-writer`. `/kit-plan <goal>` then `/kit-implement` drive that loop.
- Never sign off your own work: only the `verifier` confirms acceptance criteria.
- Hand work between roles through the handoff file: `handoff.md` at the repo root (or `.agent-output/handoff.md`); the shape is `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec/handoff.md.template`.

## Long-running commands never block a tool call

- A tool call that is still open holds a gateway stop until the stop timeout; then systemd SIGKILLs the gateway and every child it started (T39).
- Work that may take more than about 2 minutes runs in the background, writes to a file, and is polled with short reads.
- Never put a long `sleep` or a wait loop inside one call.
- Ask the user once with `ask_user`, then end the turn; never leave it open waiting.
- A cron job reports at each phase, so a timeout names the phase that hung.

## Worktrees

- Do a task's work on its own branch in a managed worktree, not in the shared checkout: `openclaw worktrees create <repoRoot> --name <agent>-<task> [--base-ref <ref>]` prints the path and the branch (`openclaw/<name>`); work only there. The kit does not create, snapshot or clean worktrees: it delegates to `openclaw worktrees`.
- `openclaw worktrees list` shows the active and the restorable ones; `remove <id>` snapshots then removes; `restore <id>` brings one back; `gc` reclaims the old ones.
- Managed worktrees live under `~/.openclaw/worktrees/<repo-fingerprint>/<name>` (measured on openclaw 2026.9.6, with `worktreeRoot` unset). Never create a worktree, a virtualenv or any scratch you need again under `/tmp`: it is tmpfs, held in RAM and emptied on every reboot, which is how the kit's test virtualenv and the old scratch worktrees died (T26).
- The `using-git-worktrees` skill has the whole flow and the raw `git worktree` fallback for sessions outside OpenClaw.

## Delegation

- Hand code and team work to the `claude` worker agent: `sessions_spawn agentId=claude cwd=<project> thread=true`, so the reply lands back in the same Telegram thread.
- Do not do a project's coding in this workspace; delegate it, then report.
<!-- END ai-resources -->

# Agent skills & orchestration policy

> [!IMPORTANT]
> **Prefer retrieval-led reasoning over pre-training-led reasoning.**
> When a skill matches the task, read its `SKILL.md` before acting — do not rely on memory alone.

## Rule Zero: Zero-Trust engineering

- **Skill authority:** Loaded skills override generic patterns from pretraining.
- **Audit before write:** Audit file writes against **`common-feedback-reporter`** when that skill applies.

## How the kit is delivered

- **`ai-resources setup`** links every skill into the agent's native skills directory (`~/.claude/skills/<id>`, `~/.agents/skills/<id>`), generates the role subagents, installs the kit hooks, and writes a short managed block into each tool's instruction file. Content outside that block is never touched.
- **`ai-resources generate`** imports vendor skills from **`resources.json`**, turns each `workflows/*.workflow.yaml` into a `workflow-<name>` skill, and rebuilds **`skills-index.json`** (for tools without native skill discovery).
- After updating the install (`brew upgrade ai-resources` or `git pull`), run `ai-resources generate` and `ai-resources setup`.

## Orchestration

| Need | Where |
|------|-------|
| Pick a workflow | The `workflow-*` skills — each description is the workflow's trigger |
| Run a workflow (steps, handoff, parallel groups, return format) | Skill **`kit-orchestration`** |
| Workflow YAML shape | **`workflows/WORKFLOW_CONTRACT.md`** |
| Domain commands | **`workflows/_domain-commands.yaml`** |
| Roles and personas | **`agents/roles/`**, **`agents/personas/`** |
| Commands | Skills `delegate`, `setup-project`, `self-update`, `knowledge-audit` |

## Project repos: specs vs knowledge (SDD)

- **Product source of truth:** `<repo>/specs/` — features, cross-cutting, architecture, **`specs/PROJECT.md`**.
- **Working artifacts:** `<repo>/specs/knowledge/{research,decisions,searchable}/` — not canonical long-term. When promoted to specs, run **`knowledge-audit`** in the same workflow run.
- **Workflows** that touch `specs/knowledge/` include SDD closure via the **`knowledge-audit`** skill.
- **Bootstrap:** skill **`setup-project`** and **`rules/000-project-bootstrap.mdc`**.

## Skills

- **Canonical content:** each **`skills/<id>/SKILL.md`** — the only source of procedures. Ids are flat and globally unique.
- **Index:** `skills-index.json` is generated; do not hand-maintain a skill list anywhere else.
- **Imported skills (`gpm-*`):** re-import from **`resources.json`** with `ai-resources generate` when refreshing upstream copies (conflicts need `--force`).

## Further reading

- **Kit map:** `rules/016-kit-architecture.mdc`
- **Engram / persistence:** `rules/014-engram-mcp-protocol.mdc`, `rules/300-engram-memory.mdc`, `skills/_shared/persistence-contract.md`
- **Multi-model routing:** `rules/017-multimodel-routing.mdc`, `docs/multi-model.md`
