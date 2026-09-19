# AGENTS.md — @AGENT_NAME@

> Read this first. OpenClaw injects this file into every turn of the `@AGENT_ID@` agent.

## Why this file and not CLAUDE.md

OpenClaw starts Claude Code with `--setting-sources user`, so **project-level `CLAUDE.md` files
and project settings are invisible to this agent**. Only the user-level Claude config and this
file reach you. Every rule that must bind this agent lives here or under `~/.claude`; a rule
written only in a repo's `CLAUDE.md` does not exist for you (T02). When this file and a repo's
`CLAUDE.md` disagree, this file wins, and the discrepancy is worth reporting.

## Role

This is an **umbrella workspace**: a directory that groups several repositories. You work on the
projects inside it, one repository at a time.

## Map: subproject, repository, docs home

Fill this in once; it is what lets you find the right repo and the right place for a doc.

| Subproject | Repository (remote) | Docs home |
|---|---|---|
| _example-service_ | _git@host:org/example-service.git_ | _docs/ in that repo_ |

## Never commit at the umbrella level (T11)

The umbrella directory is NOT a repository you commit to: it has no commits of its own, and a
`git init` or a `git add` there would create a second, competing history. Every change is made
and committed **inside the repository that owns the files**. Before any `git` command, run
`git rev-parse --show-toplevel` and confirm you are where you think you are.

## Durable documentation

Anything worth keeping goes in the repository's docs (ADRs, runbooks, specs, READMEs), committed
with the change that needs it. **Never leave the only copy under `.agent-output/`**: that
directory is scratch space, ignored by git, and lost with the workspace.

## Working as a team

For anything beyond a small edit, work as a team with the kit's roles rather than doing it all
in one head: `planner` (plan), `software-architect` (design and validation), `implementer`
(code, test first), `tester` (run and analyse), `code-reviewer` and `security-auditor`
(independent review), `verifier` (sign-off) and `doc-writer` (durable docs). The workflow
scripts `/kit-plan <goal>` and `/kit-implement` drive that loop. Hand work between roles through
the handoff file, and do not sign off your own work: the verifier does.

## Memory

Use Engram (`mem_search` before you start, `mem_save` after a decision, a bug fix or a discovery,
`mem_session_summary` before you stop). OpenClaw's own memory tools are not the record for this
work.

## Red lines

- **English for every artifact**: code, comments, commit messages, branch names, PR titles and
  bodies, ADRs, runbooks, READMEs, tickets. The conversation follows the language of the person
  you are talking to; the artifacts do not. User-facing copy in another language is the only
  exception.
- **No AI attribution in commits or docs**: no `Co-Authored-By` trailer for an assistant, no
  "generated with" line, no assistant signature in a commit or a PR description.
- **Explicit `--context` on every cluster command**: `kubectl --context <name> ...`,
  `flux --context <name> ...`, `helm --kube-context <name> ...`. The active context changes by
  itself (a credentials refresh rewrites it), so an ambient context is never trusted. Read
  before you write, and confirm the target before any `apply`, `delete`, `patch` or `reconcile`.
- **Secrets never go into a file, a commit or a message.** Point at where they live.
- **Ask before anything irreversible** (force-push, delete, drop, rotate) unless this file
  says otherwise.

