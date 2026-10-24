# ADR-0016: Delegated cockpit auth, live state as the source of truth, one operator-rules file (round 4)

**Status**: Accepted (2026-10-23, the operator's answers in plan section 9 are binding); implemented on `feat/setup-round4-delegated-auth` for the single 2.6.0, which the operator tags. The independent Verifier has not signed off yet.
Plan: `.agent-output/planner/setup-round4-delegated-auth-plan.md` (section 9 carries the binding operator answers).
**Date**: 2026-10-23
**Decision makers**: operator (plan section 9, binding), implementer (record only).
**Supersedes**: the auth parts of [ADR-0015](0015-per-cockpit-setup-openclaw-orchestrator.md): the D1 table (methods stored per
cockpit as the source of the sign-in) and the D2 provider question (the kit collecting API keys for OpenClaw providers).
**Constraint, not amended**: [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md). Setup never restarts the live
gateway, never calls it, and never runs a restart-required change. Tests never run the real `openclaw`, `claude` or `agy`.
**Does not amend**: [ADR-0001](0001-model-pins-and-auto-update.md) (fill-only), gates G0-G7, the gate record file and the ack
hash algorithm of [ADR-0007](0007-subscription-lanes.md) section 11 and [ADR-0010](0010-setup-five-phases-channels-team.md).

## Context

Round 3 made setup a collector: it asked for a sign-in method per cockpit, stored the answer, and used the stored answer
afterwards. On the operator's host this produced wrong results: a live OpenClaw with every agent on `anthropic/claude-sonnet-5-5`
was offered `agy-cli/gemini-*` because the saved engine said antigravity; Review said "API key" for Claude Code with no key in
the environment; agy lanes read "(on)" through a host overlay although no gate evidence existed; a channel token travelled on
argv; and hygiene, the agy MCP file and the CLAUDE.md migration wrote to disk before Apply.

## Decision

### D1. Auth is delegated to each cockpit's own flow

Setup shows the LIVE auth state and asks keep or change. Change hands the terminal to the cockpit's native flow, with inherited
stdio, only on that explicit choice and never in `--non-interactive` mode or tests:

| Cockpit | Native flow | Kit extra (only where the tool has no flow) |
|---|---|---|
| Claude Code | `claude` `/login` | `apiKeyHelper` in settings, pointing at a user-owned 0600 file or a command; the kit stores only the path/command |
| OpenClaw | `openclaw configure --section model` | none |
| agy | `agy` login | none |

After the handover the live state is re-read and the LIVE kind is recorded, never the answer. "API key" is active only when
`ANTHROPIC_API_KEY` or `apiKeyHelper` is present. The kit removes its own key prompt, its `<provider>:manual` profile and the
image gate's own sign-in. `claude auth status` fields other than the method are dropped on read (no email, no org id, no
account id); key values are never read, only presence. API-key, Vertex and Bedrock output shapes of `claude auth status` are
unverified and classify as `unknown` unless the environment or settings prove otherwise. `google-vertex` is shown as ADC /
project / location or the `gcp-vertex-credentials` sentinel, never as an API-key option. The ADR-0015 `cockpit_auth` table
survives as fill-only defaults and never overrides a live signal.

### D2. Live config beats saved state

The defaults of the OpenClaw engine and model questions are the live primary, fallbacks and runtime read from `openclaw.json`.
Saved state fills only where live is empty. Any difference between live and chosen is rendered "old -> new" and needs an
explicit confirm; without it the patch omits `agents.*.model` and the runtime entirely. The engine auto-add of "google (CLI
backend)" is removed. The confirmed `kit-lane-agy-*` drift migration (anthropic/ or claude- prefix) keeps working.

### D3. Held agy lanes, honest Google terms

The host overlay of the gate-record module is removed: absent evidence means the gates are not proven and every agy lane reads
"held" with the refusal reason, never "(on)". A valid record (made only by `ai-resources lanes gates record`) reads "on".
`ACK_TEXT['google']` states plainly that the OpenClaw docs (providers/google.md) say the Antigravity terms prohibit third-party
tools using Antigravity OAuth, that the operator accepts the suspension risk, and that G0-G7 still decide admission. The
text hash changes, so a stored Google ack is rejected by `valid_acks`; setup prints a one-time "terms text changed, re-confirm"
notice and fails closed. It never re-signs by itself. See the addendum under ADR-0007 section 11.

### D4. One operator-rules source, three cockpits

`~/.config/ai-resources/operator-rules.md` (XDG-aware, created only if absent, seeded from the operator's CLAUDE.md after
confirmation, scanned by `secret_findings`) is rendered as a managed block into `~/.claude/CLAUDE.md`, the AGENTS.md of every
OpenClaw agent workspace the kit owns, and `~/.gemini/GEMINI.md`. Fill-only inside the markers, idempotent. The seed runs before
the CLAUDE.md migration, which also offers to copy a moved block that belongs to an operator-rules category.

### D5. Review shows who runs and who is billed

Separate columns "Runs in" (Claude Code subagent, OpenClaw kit-role agent, agy) and "Billed to" (subscription, API per token,
Google plan, unknown), plus model and effort, for every role. Unknown is shown, never guessed. Each cockpit gets one live auth line.

### D6. All writes are deferred to Apply

Hygiene, the agy MCP file, the CLAUDE.md migration and the operator-rules writers enqueue `Action`s (`setup/deferred.py`); Review
lists them under "Will write"; Apply runs them in order; dry-run lists without executing; Ctrl-C before Apply leaves zero diff.
Only the native auth handover is immediate and is labelled "applies now". Downstream readers use the planned state.

### D7. Masked channel token, honest rail

The channel re-enable token is prompted with `ui.password` and passed through the child environment with `--use-env` where the
channel declares it (env is readable by same-user processes through /proc: weaker than a file, better than argv). Otherwise the
terminal goes to interactive `openclaw channels add`. The rail uses the five `PHASE_LABELS`; removed-step labels and the dead
provider and credentials step functions are deleted (the unreachable lifecycle step stays: tests still exercise it).

## Consequences

- On this host agy lanes are held until the operator runs `ai-resources lanes gates record`. Intended.
- The operator re-confirms the Google terms once.
- Order of effects changes because writes are deferred; readers consult the planned state.
- Rollback: one commit per step; reverting the D3 commit restores the overlay.

## Addendum (2.6.2): the runtime is pinned per agent, never in the shared catalog

OpenClaw resolves a runtime from the agent's own exact `models` entry first, then from `agents.defaults.models` (exact
before the `provider/*` wildcard). The shared catalog is every agent's, so a `claude-cli` pin there moves the operator's
agents too; and when the ref was already listed (as `anthropic/*` is on an API host) the kit wrote nothing and its lanes ran
on the API. From 2.6.2:

- The kit's own agents (`kit-role-*`, claude-cli `kit-lane-*`) get `claude-cli` in their own `models` map, fill-only. A role
  on a Claude model runs through Claude Code when Claude Code is installed and not signed out, whatever OpenClaw's own
  Anthropic sign-in is: the kit team runs on the operator's CLIs (ADR-0015). Review bills those rows to the Claude Code sign-in.
- The `claude` worker is the operator's agent: setup offers to pin it to `claude-cli` (default No, never in a dry run or a
  non-interactive run) and records the change.
- The operator's agents are not touched: they keep OpenClaw's route, for example an API key whose organization receives the
  monthly Claude Platform credits of a Max plan.
- The lanes restore removes the pins the kit recorded (`runtime_pins_added`); catalog refs an older run recorded are still
  removed.
