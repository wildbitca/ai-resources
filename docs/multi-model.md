# Multi-Model Routing — User Guide

## Words the setup uses

`ai-resources setup` asks three things: **which tools do you use**, **how should they work**, and **who does what**.
Every AI coding CLI (Antigravity, Aider, Claude Code, Codex, Continue, Copilot, Cursor, OpenClaw, OpenCode, Windsurf)
is a **tool**; Claude Code is one of them, listed like the others. The rest of this guide, and the setup screens on the
default path, use six words:

| Word | Meaning |
|---|---|
| Tool | Any AI coding CLI the kit can see. Installed ones are found for you; tick a missing one to install it. |
| Role | A job in the team: explorer, planner, architect, implementer, reviewer, security, verifier, tester, docs. |
| Team | Which tool does which role. |
| Starting team | Balanced, Performance, Low cost or Custom: a ready-made team you can start from. |
| Subscription | A plan you already pay for (Claude, Google, OpenAI). Signing in with it asks for no API key. |
| Sign-in | How one tool authenticates, asked per tool (ADR-0015): Claude Code with a subscription, an API key or Vertex AI; OpenClaw with the providers you pick, each by API key or CLI backend; Antigravity with your Google account. |
| Gateway | **No longer offered** (ADR-0015). A gateway installed earlier keeps running and is never removed by setup; a saved `advanced` answer is migrated to the tools' own sign-in, shown once as old -> new. |

Plain phrases stand in for the internal names: "Sign-in" is the per-tool *cockpit auth* (it replaced the old "How should they work?" *mode* question in ADR-0015), "Parallel runs" are the
*subscription lanes*, "Terms check" is the *acknowledgement* (the stored text is unchanged), "Start at login" is the
*lifecycle*, "API keys" are the *credentials*. The old words stay where they are exact: state keys (`mode`, `backend`,
`cli_team`, `profile`, `lanes`, `lane_acks`, `cockpits`), CLI flags, file names (`executors.yaml`, `lanes.yaml`), the
ADRs, `doctor` and `verify` output, and the internal sections of this guide below. Design:
[ADR-0009](decisions/0009-setup-tools-how-team.md).

---

Multi-model routing lets each kit subagent run on a different LLM. Two backends
serve it, and the wizard asks which one you want:

| Backend | What it is | You install | Credential |
|---|---|---|---|
| **OpenRouter** | A hosted gateway reaching 400+ models | nothing | one key |
| **LiteLLM** | A gateway this kit installs and supervises on your machine | a local service | one key per provider |

Neither is a default in disguise. OpenRouter is the shorter path; LiteLLM is the
one that reaches local models through Ollama and enforces per-key budgets
itself.

> **A gateway replaces your claude.ai subscription for Claude Code.** While a
> gateway credential is active the subscription is not used and every token is
> billed to the gateway. That applies to both backends, and the wizard says so
> before you choose.

## Quick start

```sh
ai-resources setup       # interactive wizard — five phases (Tools, Cockpit, Team, Audio, More)
ai-resources doctor      # verify everything works
```

The wizard finds the tools you have (Claude Code, Cursor, Antigravity CLI, …) and lets you tick more to install, asks
each ticked tool how it signs in (ADR-0015), gives each role to a tool through a starting team, and shows a review
before it writes anything.

### What the wizard looks like

Four screens, over ten internal steps. Each step starts with a header, a progress rail and one line of key hints; the
header counts screens (`[2/4]`), the rail counts the ten steps:

```
[2/5] Sign-in -------------------------------------------------------------------------------
 ✔ Tools  ● Sign-in  ○ Gateway  ○ Providers  ○ Credentials  ○ Models  ○ Cockpits  ○ Lifecycle  ○ Review  ○ Verify
 ↑/↓ move · Space toggle · Enter confirm · Ctrl-C cancel
```

**Screen 1, Your tools.** One list for every tool, one order (installed first, then alphabetical). Tick the tools the
kit should use; ticking a missing one installs it, unticking never uninstalls. Claude Code is in the same list with a
"recommended to start" label, no special position and no special tick. Antigravity is never ticked on its own: its row
says it sends repo content to Google and that a terms check comes next. Installed is not signed in: sign-in is not
checked.

```
? Tick the tools the kit should use. Ticking a missing tool installs it; unticking never uninstalls.
    [ ] Antigravity CLI (agy)  installed v1.4  sends repo content to Google; terms check next
    [x] Claude Code  installed v2.1.300  recommended to start
    [x] Codex CLI  installed v0.163
    [ ] Aider  not installed  curl -LsSf https://aider.chat/install.sh | sh
    [-] Windsurf  manual install   (manual install: https://docs.devin.ai/desktop/getting-started)
```

**Screen 2, Sign-in.** Every ticked tool shows what it reports LIVE and asks keep or change (ADR-0016); nothing is collected
by the kit. Claude Code: its own sign-in (`claude auth status`); change hands the terminal to `claude auth login`, or writes an
`apiKeyHelper` command or 0600 file path into `settings.json` (the key is never read or stored), or keeps Vertex AI's project and
region. OpenClaw: the providers each agent can use, read from OpenClaw's own auth store (`openclaw models status` and `models auth
list`); change hands the terminal to `openclaw configure --section model`. Antigravity: whether a Google account state is present;
change hands the terminal to `agy`. The terms check stays where its lanes are enabled. Cursor: managed in Cursor, nothing asked.
After a handover the live state is read again and that is what is recorded. The kit stores kind and method names and provider
ids, never a key. A native flow is never started in `--non-interactive`, `--dry-run` or by a test. There is no team question and
no gateway question; a saved "As a team / Each on its own / Advanced" answer is migrated once (see
[Signing in](#choosing-the-mode)). `--backend` is deprecated: any value pre-fills Claude Code's subscription sign-in and the
question is not asked.

**Screen 3, Your team.** One question, *Starting team*: Balanced (recommended), Performance, Low cost or Custom.
Choosing it is the confirmation; the role table and a team table (which tool takes which roles) are shown with it.
Writing code always stays with Claude Code. Antigravity shows roles only when it is ticked, Codex shows "no roles yet"
and is never a choice. Custom opens the model and role questions of earlier kits unchanged. If Antigravity is ticked, a
terms check follows (see [the Antigravity lane](#the-antigravity-lane-adr-0007-section-11-phase-3-closed)).

**Screen 4, Review.** The answers by name (Sign-in, Team, Tools, Will write) and the choices *Apply*, *Change tools*,
*Change sign-in*, *Change team*, *More options...* and *Cancel*. A *Change* jumps back to that screen with your answers as
defaults and returns here. *More options...* holds what the default path does not ask: Parallel runs (Claude Code on
your subscription, off by default) and OpenClaw (engine, host, voice; asked on the first configure only). Nothing of the kit's configuration is written before Apply.

A step that does not apply is skipped without output on the default path; a gateway path still says why. In a narrow
terminal (under 60 columns) the header is one line, `Step 4/10  Provider backends`; with `NO_COLOR` set, or on a
terminal that is not UTF-8, the rail uses `[x]`, `[>]`, `[ ]` and `[-]`.

| Key | What it does |
|---|---|
| Up / Down, Space, Enter | Move, toggle (multi-select) and confirm |
| Ctrl-B | Back to the previous step, in the steps that changed nothing yet (team, models, role assignment, parallel runs, OpenClaw questions). A step after one that installed or saved something refuses it and says so |
| Ctrl-C or Ctrl-D | Cancel, in every prompt (select, multi-select, confirm, text and password; in a text field that holds text Ctrl-D deletes the character under the cursor). Exit code 130; the message names the step and lists anything already done in the run |

The review shows credentials only as `set` or `missing` (on a gateway path) and is shown on the "Use a kit profile" path
too (a non-interactive run has nobody to ask and skips it). Installing a missing tool happens on screen 1, so Back
cannot rewind past it. For each tool installed in that run that has a sign-in you are then asked (a separate,
pre-ticked `Sign in now?`) whether to sign in; the tool's own interactive login runs on your terminal (the kit never
sees or stores a credential), and without a terminal or with `--yes` the command is printed for you to run later.
`--yes` installs only the missing ones of Claude Code, Antigravity and OpenClaw. Some earlier steps do act on the
machine when you confirm them: tool, pipx and LiteLLM installs, the LiteLLM image pull, provider credentials and the
remote master key written to the env file, the gateway state save, and the multi-model cleanup when you leave that
mode. A cancel lists whatever already happened. `--dry-run` installs and writes nothing and ends with a masked diff
per file and `DRY RUN: nothing was written`. On a re-run every question starts from your saved answer. A Ctrl-C while
a tool is being written is listed as `may be partially written: <tool>`; re-run setup to finish it. Output echoed from
installers is stripped of terminal escapes and masked.
Design: [ADR-0008](decisions/0008-setup-wizard-tui-on-rich-questionary.md), amended by
[ADR-0009](decisions/0009-setup-tools-how-team.md).

### Choosing the mode

**Since ADR-0015 there is no mode question.** Each tool asks its own sign-in (screen 2) and the old global fields are
derived from the answers: `mode` is always `single-model`, `cli_team` is on, and `cli_team_api` is on when a tool signs
in with an API key or Vertex AI. A saved answer migrates once, fill-only, and the old -> new table is shown once:

| Saved answer | Claude Code | OpenClaw | Antigravity |
|---|---|---|---|
| As a team (API keys / Vertex) | `api_key` (`vertex_adc` with a configured Vertex project) | providers by `api_key` | `google_account` |
| As a team (subscriptions), Each on its own | `subscription` | providers by `cli` | `google_account` |
| Advanced (LiteLLM, OpenRouter) | `subscription` | providers by `api_key` | `google_account` |

A gateway installed on a host stays as it is (no teardown, no removal); a gateway-only profile is replaced by `balanced`
and the run says so. Your team starts from `balanced`, and the subscription lanes ([below](#subscription-lanes)) are reached
from Review > More options > Parallel runs: Antigravity stays opt-in and gated, and the Codex lane stays held. The
`cli_team` key is absent from a state that never chose a team, so older files are unchanged.

### Where Gemini shows

Gemini models come through Antigravity (`agy`), a subscription CLI with its own login, so they are not in the "pick
models from my accounts" list, which is built from API credentials. With `agy` installed, the Custom list prints an
"Antigravity (agy, subscription)" group of informational rows (`lane, opt-in`, plus `unconfirmed slug` until a V5 run
confirms the slugs); it asks for no key and cannot be picked as an API model. On "Your team", the team table has an
Antigravity row: untick = "off", ticked = the roles it takes as PRIMARY in the routing matrix (`scoring/routing.py`, ADR-0019), each slug labelled `[unconfirmed slug]` while unconfirmed, and "sends repo content to
Google; terms check next". Without `agy`, setup says so in one line. The terms check (Google acknowledgement, gates
G0-G7) is the same one `--lanes-antigravity` opens; nothing is written to agy.

## Mental model

Claude Code and Aider are the cockpits the kit points at a gateway; the gateway
routes on the `model` field of each request. Every other cockpit keeps its own
model settings and the kit only writes instructions (and MCP) for it — see
[Which cockpit can use which model](#which-cockpit-can-use-which-model). Tools,
MCP, skills and workspace access are preserved — only the LLM behind each
subagent changes.

```
┌──────────────────────────────────────────────────┐
│  Cockpit (Claude Code / Cursor / agy …)   │
│  ANTHROPIC_BASE_URL=<gateway>                    │
└───────────────────────┬──────────────────────────┘
                        │
        ┌───────────────┴────────────────┐
        ▼                                ▼
┌───────────────────────┐   ┌────────────────────────────┐
│ OpenRouter (hosted)   │   │ LiteLLM (yours, local)      │
│ openrouter.ai/api     │   │ 127.0.0.1:4000              │
│ namespaced model IDs  │   │ bare model IDs              │
│ anthropic/claude-…    │   │ claude-…  gemini-…          │
└───────────┬───────────┘   └─────────────┬──────────────┘
            ▼                             ▼
    every provider              Anthropic · Google · Vertex
    behind one key              OpenAI · local Ollama
```

The two are not interchangeable. OpenRouter needs namespaced IDs
(`google/gemini-3.7-flash`); LiteLLM needs bare ones (`gemini-3.7-flash`). That
is why profiles declare a `backend:` and the wizard only offers the matching
set.

### What carries the model

Three things resolve a model, in order of precedence:

1. **A subagent's `model:`** — generated into `~/.claude/agents/<name>.md` from
   the profile. Claude Code forwards this value to the gateway verbatim, so any
   ID the gateway understands works here.
2. **`classes`** — pins `/model` aliases (opus, sonnet, haiku, fable) for the
   *main* conversation. Only the OpenRouter backend needs it: Claude Code
   resolves an alias to a bare Claude ID, which a namespaced catalogue does not
   recognise.
3. **The session model** — whatever `/model` is set to, for any subagent whose
   model is `inherit`.

## Roles and personas

Two kinds of subagent are generated, forty in total:

- **Roles** (`agents/roles/*.md`, 15) — planner, implementer, code-reviewer, …
  Each one takes its model from `by_role`.
- **Personas** (`agents/personas/<role>-<domain>.md`, 25) — a role plus a
  domain's conventions, such as `code-reviewer-angular`. The generated file is
  the role's body followed by the overlay, and it takes its model from
  `by_persona`, falling back to its base role.

`by_persona` is empty in every shipped profile, so personas inherit their role
until you say otherwise. Filling it in is how an infrastructure review and a
CSS review stop costing the same:

```yaml
by_persona:
  code-reviewer-devops:  { provider: openrouter, model: anthropic/claude-opus-5.5 }
  code-reviewer-angular: { provider: openrouter, model: anthropic/claude-sonnet-5.5 }
```

## Configuration files

| Path | Purpose |
|---|---|
| `~/.config/ai-resources/setup-state.yaml` | Wizard answers, including `mode` and `backend` |
| `~/.config/ai-resources/executors.yaml` | **Single source of truth** for the model mapping |
| `~/.config/ai-resources/.env` | Credentials (chmod 600, never commit) |
| `~/.config/ai-resources/litellm.yaml` | LiteLLM backend only — generated from executors.yaml |
| `~/.config/ai-resources/bin/litellm-run` | LiteLLM backend only — wrapper script |
| `~/.config/ai-resources/logs/` | LiteLLM backend only — service logs (macOS; Linux uses journald) |
| `~/Library/LaunchAgents/com.ai-resources.litellm.plist` (macOS) | LiteLLM backend only — auto-start |
| `~/.config/systemd/user/ai-resources-litellm.service` (Linux) | LiteLLM backend only — auto-start |

Under the OpenRouter backend the last five do not exist: there is no local
service, so `ai-resources daemon` has nothing to manage and says so.

## Profiles

| Profile | Mode | Description |
|---|---|---|
| `balanced-claude-55` | multi-model, either gateway | **Recommended.** Claude only, three tiers on the 5.5 family (Haiku, Sonnet, Opus). Valid in every compatibility cell it can reach without `--allow-unverified`. Needs a LiteLLM or OpenRouter gateway. |
| `measured-best` | multi-model, either gateway | The explicit multi-provider profile (Claude, Google, DeepSeek) from the 2026-09-16 measured runs. The Google and DeepSeek refs are unverified for Claude Code, and setup does not write them into the Claude subagents (those roles inherit the session model) unless you pass `--allow-unverified`. |
| `openrouter-balanced` | multi-model, either gateway | Judgement on Claude, bulk reading on Gemini Flash. Namespaced ids, written for OpenRouter. |
| `gateway/balanced` | multi-model, OpenRouter | The Balanced starting team for OpenRouter. Claude Opus with no fallback for the planner, architect, reviewer, verifier and security auditor; Sonnet for the implementer, generalPurpose and the fixers; Gemini Flash for explore, tester and doc-writer with a Claude fallback. Gemini is not verified with Claude Code through OpenRouter, so Review asks once whether to use it. |
| `openrouter-cheap` | multi-model, either gateway | Budget models throughout, except review and security. |
| `quality-first` | multi-model, either gateway | Claude for code-critical roles, Gemini for high-volume support. |
| `all-claude` | multi-model, either gateway | Every role on Claude (Sonnet for most, Opus for the architect, Haiku for tests and verification). No Gemini key required. |
| `all-gemini` | multi-model, either gateway | Every role on Gemini. Cheapest. |
| `cost-optimized` | multi-model, either gateway | Aggressively biased toward Flash and Haiku. |
| `claude-native` | single-model | Each role picks a Claude alias. No gateway. The default for single-model and for a Claude-only host with no gateway. |
| `subscription-claude-55` | single-model | Legacy: the Claude-only Balanced as it shipped before ADR-0010 S6 (no Gemini candidates). Kept so a host saved under this name keeps its team; new setups use `balanced`. |
| `claude-inherit` | single-model | Every role inherits the session model. |
| `gateway/performance` | multi-model, either gateway | The Performance starting team for the gateway modes. Claude only, valid in every compatibility cell without `--allow-unverified`: Opus for the planner, architect, reviewer, security auditor and verifier, Sonnet for every writer. Costs more than `balanced-claude-55`. |
| `gateway/low-cost` | multi-model, either gateway | The Low cost starting team for LiteLLM. Claude only and verified: Haiku for reading and mechanical roles, Sonnet for the planner, architect, code, review and verification, Opus only for security. Replaces `cost-optimized` as the preset because that profile names Google models the Claude cockpit has not verified. |
| `balanced` | single-model, no gateway | The performant and low-cost mix for a Claude subscription (retuned 2026-10-22, ADR-0014): Haiku for explore, tester, doc-writer and package-upgrade; Sonnet for the planner, architect and writers; Opus for the verifier and security auditor only, so checkers never share a family with writers and nothing falls back to Opus. A host that saved it sees the old -> new table once. Not recommended as the default; aliases float, so the evidence may drift. |
| `performance` | single-model, no gateway | The Performance starting team without a gateway. Claude aliases: Opus for the planner, architect, reviewer, security auditor and verifier, Sonnet for writers and the explorer. Checkers stay on Opus and no writer does. Carries a lane table. |
| `low-cost` | single-model, no gateway | The Low cost starting team without a gateway. Claude aliases: Haiku for exploration, docs and upgrades, Sonnet for the planner, architect, writers and reviewer, Opus only for the verifier and security auditor (independence is not traded for price). Carries a lane table. |

Custom: pick `custom` in the wizard to override each role individually.

### Choosing models: the starting team

The question of "Your team" (step 6) is one plain choice, with no provider, vendor or model in it:

| Starting team | What it means |
|---|---|
| Balanced (recommended) | Strong models where defects are caught, cheaper ones for reading and mechanical work. |
| Performance | The strongest models on planning, review and verification. Costs more. |
| Low cost | The cheapest models that still keep review, verification and security strong. |
| Custom | The earlier flow: pick a profile, pick models from your accounts, customize any role, or compose a team. |

A starting team resolves to a kit profile by how the host reaches its models, then the wizard shows the role table
(and, as a team, who does what) with the choice: choosing it is the confirmation, nothing is asked again. There is no
"Customize any role?" on this path.

| Starting team | As a team | Each on its own | Advanced: LiteLLM | Advanced: OpenRouter |
|---|---|---|---|---|
| Balanced | `balanced` | `balanced` | `balanced-claude-55` | `gateway/balanced` |
| Performance | `performance` | `performance` | `gateway/performance` | `gateway/performance` |
| Low cost | `low-cost` | `low-cost` | `gateway/low-cost` | `gateway/low-cost` |

Preset ids are the same on every row (ADR-0010). The old names keep loading forever and resolve to the same team:
`subscription-performance` is `performance`, `subscription-economy` is
`low-cost`, `performance-claude-55` is `gateway/performance` and `economy-claude-55` is `gateway/low-cost`. A saved
state under an old name is saved under the new one on the next real run, with one line saying so. `subscription-claude-55`, `claude-native`,
`balanced-claude-55` and `openrouter-balanced` are not aliases: their content differs, so they keep their own file and
the question only opens on Balanced for them.

**Balanced is multi-provider (ADR-0010).** Checkers never share a family with a writer, in every shipped profile
(the verifier and security-auditor run on Opus with no fallback onto a writer's family). On the CLI row Balanced adds
Gemini over Antigravity as the PRIMARY of `explore`, `code-reviewer`, `doc-writer`, `tester`, `package-upgrade` and
`generalPurpose` (the routing matrix, ADR-0019; Antigravity may write). The verifier and the security-auditor take no
Gemini; the planner, architect, implementer and the fixers have a Claude primary. On OpenRouter (`gateway/balanced`)
explore, tester and doc-writer run on `google/gemini-3.7-flash` with a Claude fallback and everything else on Claude.
Adding the planner and the software-architect to the Google acknowledgement is a re-sign of that role list (ADR-0007
section 9): an acknowledgement stored without them is asked again through the "Extend the Google acknowledgement to"
prompt (default No); the acknowledgement text itself is unchanged.

What you see for Gemini, from one set of lines (`setup/team_view.py`), and the table says the real provider and lane of
every role:

| Row | Gemini line |
|---|---|
| As a team, Antigravity ticked and acknowledged, gates not proven | `Gemini held: Antigravity gates 0/8 proven (G6, G7 partial). Next: run the probe window, then ai-resources lanes gates record`; the cells read `google <slug> via Antigravity [unconfirmed slug] (held)` |
| As a team, gates proven and the roles acknowledged | the cells read `google <slug> via Antigravity (on)`, no tag, no held line; writers stay on Claude Code |
| As a team, Antigravity not ticked | `Gemini not active: Antigravity not selected` |
| Each on its own | `Gemini not active: 'Each on its own' runs Claude Code only (choose 'As a team' with Antigravity, or OpenRouter)` |
| OpenRouter, opt-in declined | `Gemini held on OpenRouter: Claude Code x Google not verified; explore, tester, doc-writer use the session model` |
| LiteLLM (saved, no longer offered to new setups) | `Gemini not active: LiteLLM is legacy; choose OpenRouter for Gemini`; a `google/*` role with no enabled Google provider runs on its Claude fallback |

The OpenRouter opt-in is asked once in Review (`Use Gemini on explore, tester and doc-writer through OpenRouter (not yet
verified with Claude Code)?`, default No, never answered for you, not by `--yes`); Yes records `profile.allow_unverified`.
A new user's Antigravity tick starts off, and a saved tick is restored only when a valid stored Google acknowledgement
exists. The one operator action that turns the Antigravity cells on is the probe window plus
`ai-resources lanes gates record` and its commit ([runbook](openclaw/runbook.md#the-probe-window-and-the-one-record-command-adr-0010-n3)).

**Channels.** When OpenClaw is a tool, setup lists the channel accounts OpenClaw reports (`openclaw channels list --all
--json`, read-only) and you untick the ones that should be off; the default always comes from the current listing and no
choice is saved. Apply turns an unticked account off with `openclaw channels remove --channel C --account A` (never
`--delete`, never a credential flag, never `--agent`) only on a terminal, not under `--dry-run` or `--yes`, with the
OpenClaw cockpit, with reload rules that call `channels.<C>` hot, after a fresh listing still shows it on, a per-change
confirm (default No) and, for Telegram, a second one. The undo is to re-add the account with credentials, yourself; an
enable is only ever printed. The kit still never patches `channels.*`.
The question is skipped, and the flags path is unchanged, with `--profile`, `--models`, `--shape`, `--providers`,
`--compose` or `--non-interactive`. A saved choice is the default of the question and is never replaced: a saved preset
profile defaults to its preset, and a customized profile, a composed team, a team picked from your accounts or any
other saved profile defaults to Custom. The state file records only the profile name; it gains no field.

A host that has not chosen a profile is offered `balanced-claude-55` in multi-model mode and `claude-native` in
single-model mode. A profile a host already saved is never replaced: the wizard shows a one-line notice and, if
you ask, the role-by-role difference, and a non-interactive run prints the difference and the exact command
(`ai-resources setup --profile balanced-claude-55`) without switching.

### Recommended per-role assignment

`balanced-claude-55`, and why. "Measured" means a run in this repository's own history; everything else is
reasoning and third-party reports, and the 5.5 family itself has not been measured here (the catalogue lists the
ids and no price).

| Tier | Roles | Why |
|---|---|---|
| Haiku 5.5 | `explore`, `tester`, `package-upgrade` | Reading-heavy and mechanical: context in, summary out. The cheapest tier that supports tool use. |
| Sonnet 5.5 | `implementer`, `code-reviewer`, `verifier`, `doc-writer`, `generalPurpose`, `crashlytics-fixer`, `sentry-fixer`, `terraform-maintainer`, `crossplane-upjet-maintainer` | Code, review and verification. The implementer on Sonnet wrote the code that passed every adversarial case in the 2026-09-16 runs (measured). Review and verification stay on Sonnet or above, as rule 017 requires. |
| Opus 5.5 | `planner`, `security-auditor`, `software-architect` (fallback Sonnet) | Judgement: the planner sets the acceptance criteria everything else is measured against. |

"Architect on Sonnet for small changes" cannot be expressed: `by_role` is static. The architect ships on Opus
with a Sonnet fallback; override it per host with `ai-resources executors set software-architect`.

**Main conversation model.** The kit does not set the main conversation model under LiteLLM or single-model, and
sets the `classes` aliases only under OpenRouter. On Claude Code, `/model haiku` is the measured cheap
orchestrator (a Haiku-directed run cost 77% less than an Opus-directed one and took 12 turns instead of 37).
That is Claude Code only: OpenClaw orchestrators stay on Sonnet, because on Haiku the orchestrator announced work
it had not done (see the OpenClaw runbook, "Why sonnet and not haiku"). The Haiku measurement predates the 5.5
generation and the audit fixes below (message-id dedupe, subagent transcripts); re-measure it with
`ai-resources audit snapshot <label>` before and after a change, and compare with `ai-resources audit compare`.

**Claude-only host with no gateway.** A multi-model profile, even an all-Claude one, needs a LiteLLM or OpenRouter
gateway. With no gateway use single-model mode and `claude-native`: each role picks a Claude alias and Claude Code
needs nothing else.

### Opt-in: cross-family planner, explore and reviewer (fields exist, live behaviour NOT VERIFIED)

An optional layer for a team that wants a second model family on the roles where a different set of blind spots
helps: `explore`, `planner` and `code-reviewer`. It is a workflow choice, not a profile: those steps carry
`backend: openclaw-agent` and an `agentId` (ADR-0005, `workflows/feature-cross-provider.workflow.yaml`), and the
model is whatever that OpenClaw agent runs.

- The model is set per **agent**, not per step: there is no `model` field on a step. Point `agentId` at an agent
  configured on the model you want.
- For explicit `openclaw-agent` steps (ADR-0005), write-capable roles are rejected by the validator and the
  implementer stays on Claude Code; `tester` and `doc-writer` pass with a warning. For `backend: lane` steps the
  ADR-0007 invariant applies instead: one landing path, the kit; any role may run on any lane. See
  "Subscription lanes" below.
- A Gemini 3.8 Flash agent draws on a separate quota pool from the Claude one (Antigravity's Gemini pool and its
  Claude and GPT pool are separate; see the quota section below), so it spares the Claude quota.
- A reviewer from another model family has different blind spots. That is reasoning, not a measurement: the
  evaluation that would decide it (recall and precision of reviewer candidates on seeded defects, from the
  `ai-resources audit` cost breakdown) has not been run, and no `by_role` entry changes on its account.
  `ai-resources audit --all-projects --openclaw` now reports the API-equivalent cost per model, agent and origin, and
  `ai-resources audit snapshot` / `compare` give the before/after deltas such an evaluation needs (ADR-0013).
- agy model labels (`gemini-3.8-flash-low`, `model_pins.AGY_STATIC`) are **not** API ids and must not be mixed up
  with the `google/gemini-*` ids of `measured-best`: the first run through an agy-backed agent, the second through
  a gateway.

Status: the step fields, the validator and the example workflow ship; the live behaviour of `sessions_spawn`
with an explicit `cwd`, completion signalling and an agy-backed agent have not been run on a host (the ADR's
verification tables are empty). Treat the layer as unusable until they are.

## Subscription lanes

Reachable from "As a team" (see [Choosing the mode](#choosing-the-mode)) as well as from "Each on its own". In the
wizard they are called **parallel runs**: not asked on the default path, they are Review > More options > Parallel
runs (off by default). Ticking Antigravity on "Your tools" is the interactive opt-in for its lane and does not switch
the Claude parallel runs on.

A lane is a place a role can run: Claude Code, or an OpenClaw agent (`kit-lane-*`) on the Claude CLI backend, so a
role can use the Claude subscription login in its own process with its own worktree. Lanes are a separate pass
after the model choice (ADR-0007): they never change which model a role uses, only where it runs. They are
**disabled on a fresh install** and without a flag; a claude-cli lane draws on the same Claude subscription pool, so
it buys isolation and parallelism, not quota.

```sh
ai-resources setup --lanes --lanes-orchestrator main     # propose the table, sign the acknowledgement, write
ai-resources setup --lanes --lanes-dry-run               # show the proposed table, write nothing
ai-resources setup --no-lanes                            # clear lanes.yaml and the lane agents the kit created
```

- **What is written.** `~/.config/ai-resources/lanes.yaml` (rendered, setup-owned, both modes; `executors.yaml` is
  byte-identical with and without lanes), and, operator-run only, the `kit-lane-*` agents, their catalog entries
  and their ids in `subagents.allowAgents` of the named orchestrators. Setup never spawns a session.
- **Acknowledgement.** One per account and role list: sharing one Claude login between the orchestrator and lane
  children is a grey zone of the Anthropic terms, and non-interactive use may be billed differently. Non-interactive
  `--lanes` without a stored acknowledgement refuses. `--allow-unverified` implies nothing.
- **Kit-only landing path.** `ai-resources lanes prepare | verify | integrate | land | count` is the only way a lane
  writer's work reaches the run tree; each lane writer works in its own per-attempt worktree and no agent merges or
  lands. The procedure is in `skills/kit-orchestration/references/backends-and-lanes.md` ("Lane steps").
- **Checker independence.** The verifier and security auditor never run a model family that a writer in the run
  uses; resolution refuses it and `lanes prepare` re-checks it at dispatch.
- **Drift.** When a role's model changes (`models update`, an alias moving) the lane agent is marked `stale`, the
  lanes pass refuses it, and `status`, `verify` and `models update` print a warning; re-run `setup --lanes`.
- **Profile.** `balanced` (successor of the legacy preset `subscription-claude-55`, which keeps its own Claude-only team) declares a lane table; compose never emits a lane.

### The Antigravity lane (ADR-0007 section 11, phase 3; closed)

Antigravity (`agy`) is a peer lane backend, `agy-cli-lane`, under the same contracts, kit commands, ledger and
gates as a claude-cli lane. It is **off on a fresh install and stays closed** until all of these hold; each one is
an enforced check that fails closed with a message, not prose:

```sh
ai-resources setup --lanes --lanes-antigravity --lanes-orchestrator main   # sign the Google acknowledgement
ai-resources setup --no-lanes-antigravity                                  # close it again; claude-cli lanes stay
```

Interactively there is a second way in (ADR-0009): **tick Antigravity on "Your tools"**. The tool is never ticked on
its own, even when installed. Ticking it opens the same Google terms check after "Your team" (the text of
`lanes_render.ACK_TEXT['google']` verbatim, the account named, the roles ticked with the implementer unticked) and an
accept stores the same `lane_acks['google']` and the Antigravity gate as the flag. A decline, an empty account, Ctrl-B
or Ctrl-C unticks the tool and stores nothing; unticking a tool that was on closes the gate again (like
`--no-lanes-antigravity`). It does not turn the Claude parallel runs on. Unattended runs, `--yes` and the flags behave
as before: `--non-interactive --lanes-antigravity` without a valid stored acknowledgement still exits 2. The gates
below still decide whether anything is admitted.

- ADR-0007 section 11 is Accepted (`lanes_render.AGY_LANE_ADR_ACCEPTED`, pinned to the ADR's status line);
- `--lanes-antigravity` is on (persisted as `lanes.gates.antigravity`);
- a **valid Google acknowledgement** lists the role: one per Google account, the account is named, its stored hash
  equals the current text (a text change means re-signing), the implementer is unticked by default (a default is not
  a ban); per-account role sets: the Anthropic acknowledgement covers the roles that run on a claude-cli lane, the
  Google one only the roles with an agy candidate;
- every phase 3 gate G0-G7 is proven (`scripts/ai_resources/agy_lane_gates.py`). **Not proven today:** G0 (the
  phase 2 merge gate), G1 (V5, the `agy models` slugs; `model_pins.AGY_LANE_MODELS` holds unconfirmed candidates),
  G2 (V6, sandbox), G3 (V7, tool and MCP surface), G4 (V9, agy workspace and cwd) and G5 (V12, agy home); G6 and G7
  are partial. They run only through the isolated probe harness in an operator-announced window.

Until then no agy agent reaches state, `lanes.yaml`, `openclaw.json` or `allowAgents`, and `WRITABLE_BACKENDS` stays
`{claude-cli}`. What is facts today, whatever the gates say:

- **Process flags** across `claude-cli`, `claude-kit`, `agy-cli`, `agy-cli-lane` are recorded as facts in ADR-0007
  section 11; an agy child runs with `--dangerously-skip-permissions` (a full shell).
- **Egress.** Repository content, diffs, test output and MCP results of every acknowledged role go to Google.
- **Placement follows the matrix and live pool usage.** `lanes.yaml` carries the routing matrix only (role -> primary
  -> fallback chain, ADR-0019); a pool at or above 85% hands its work to the role's fallback. There are no static weights.
- **Best model first.** A candidate on the role's exact model (tier 1) beats a same-family other version (tier 2);
  tier 2 serves a writer only when no tier-1 candidate is admissible and is never taken by a checker. A candidate
  listed in the profile's `lanes.pins.<role>` ranks first for that non-checker role only. `effective_ids` come from
  `model_pins.effective()` and can lag Claude Code's own alias resolution.
- **Shared `~/.gemini` or a kit-owned agy home** (`~/.local/share/ai-resources/agy-home/`, never created by setup
  until V12 is proven): see the runbook.
- **Cost and breaker.** About 50k tokens per step. A per-pool circuit breaker, fed only by `lanes verify
  --spawn-error <file>` (what the orchestrator witnessed, never the child's report), refuses further lanes on an
  exhausted pool for the rest of the run. There are no pool caps by default, and never on checkers.
- **Symmetric controls.** The extended tripwire list, the `crontab -l` and user-timer snapshots, bootstrap files and
  the run-pinned `lanes.yaml` hash apply to every lane.

Operations: [openclaw runbook](openclaw/runbook.md#subscription-lanes). Team rules:
[team-composition.md](team-composition.md#lanes-pass).

## Composed teams

`ai-resources setup` can propose a team from the dated scores database instead of a fixed profile: step 6 has a
"compose" choice, and `--compose uniform|single-provider|mixed` does it without prompts. Every pick comes with a
why table; mixed mode is offered only with `--allow-unverified` and every non-Anthropic cell is marked, because
no cross-provider compat cell is verified. The result is stored as customizations over `balanced-claude-55` and
never carries `backend`, `agentId` or a cross-vendor fallback. Flags: `--compose`, `--headroom pool=level,...`,
`--headroom-live pool,...`, `--why-json PATH`, `--allow-fable`. Antigravity is direct use only and is never
composed. Details: [team-composition.md](team-composition.md).

## Editing the mapping

```sh
ai-resources executors show              # display current mapping
ai-resources executors edit              # open executors.yaml in $EDITOR
ai-resources executors set implementer   # reassign one role (or persona)
ai-resources executors test implementer  # round-trip that model
ai-resources executors apply quality-first
```

`set` and `test` accept a persona name as readily as a role, since a persona is
an executor in its own right.

After editing `executors.yaml` directly:

```sh
ai-resources setup            # re-runs the wizard, picks up edits
ai-resources daemon restart   # LiteLLM backend only, if just the gateway changed
```

## Which cockpit can use which model

The setup wizard lets you pick models in one of three shapes — **one model**,
**several models from one provider**, or **models from several providers** — and
shows, per detected cockpit and *before* writing anything, exactly what will be
configured: `configure` (the kit points the tool at the model or its auth),
`via gateway` (multi-model only), `instructions only` (the tool keeps its own
model settings) or `skip` (with the reason). It never configures a cockpit with
a model that cockpit cannot use. Kit content (skills, hooks, instruction
blocks) is written for every detected cockpit; the table governs the *model
setting* only.

A cell marked `(unverified)` has a code path that is not verified end to end:
it behaves as `skip` ("not verified yet") unless you pass `--allow-unverified`,
and the summary labels it. The table below is generated from
`scripts/ai_resources/setup/compat.py` and a test keeps it identical to the code.

<!-- compat-matrix:begin -->
| Cockpit | Mode | anthropic | google | vertex | openai | ollama | deepseek | moonshot | Source |
|---|---|---|---|---|---|---|---|---|---|
| claude | single-model | configure | skip | skip | skip | skip | skip | skip | claude.py:85, 98-100, 361-364 |
| claude | multi-model:litellm | via gateway | via gateway (unverified) | via gateway (unverified) | via gateway (unverified) | via gateway (unverified) | via gateway (unverified) | via gateway (unverified) | claude.py:318-319, 358-360; litellm.py:117-129 |
| claude | multi-model:openrouter | via gateway | via gateway (unverified) | skip | via gateway (unverified) | skip | via gateway (unverified) | via gateway (unverified) | claude.py:264-282, 321-352; providers.py:140-158 |
| gemini | single-model | instructions only | configure | instructions only | instructions only | instructions only | instructions only | instructions only | gemini.py:25-69 |
| gemini | multi-model:litellm | instructions only | configure | instructions only | instructions only | instructions only | instructions only | instructions only | gemini.py:25-69 |
| gemini | multi-model:openrouter | instructions only | configure | instructions only | instructions only | instructions only | instructions only | instructions only | gemini.py:25-69 |
| cursor | single-model | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | cursor.py:24-51 |
| cursor | multi-model:litellm | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | cursor.py:24-51 |
| cursor | multi-model:openrouter | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | cursor.py:24-51 |
| codex | single-model | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | codex.py:23-43 |
| codex | multi-model:litellm | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | codex.py:23-43 |
| codex | multi-model:openrouter | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | codex.py:23-43 |
| aider | single-model | skip | skip | skip | skip | skip | skip | skip | aider.py:37-51, 79-82; litellm.py:136-202 |
| aider | multi-model:litellm | via gateway | via gateway | via gateway | via gateway | via gateway | via gateway | via gateway | aider.py:37-51, 79-82; litellm.py:136-202 |
| aider | multi-model:openrouter | via gateway | via gateway | skip | via gateway | skip | via gateway | via gateway | aider.py:47; profiles.py:106; docs: openrouter.ai/docs/quickstart (/api/v1); trusted from documentation, not live-verified |
| copilot | single-model | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | copilot.py:23-30 |
| copilot | multi-model:litellm | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | copilot.py:23-30 |
| copilot | multi-model:openrouter | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | copilot.py:23-30 |
| windsurf | single-model | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | windsurf.py:23-30 |
| windsurf | multi-model:litellm | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | windsurf.py:23-30 |
| windsurf | multi-model:openrouter | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | windsurf.py:23-30 |
| continue | single-model | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | continue_dev.py:23-30 |
| continue | multi-model:litellm | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | continue_dev.py:23-30 |
| continue | multi-model:openrouter | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | continue_dev.py:23-30 |
| opencode | single-model | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | opencode.py:23-30 |
| opencode | multi-model:litellm | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | opencode.py:23-30 |
| opencode | multi-model:openrouter | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | instructions only | opencode.py:23-30 |
| openclaw | single-model | configure | skip | skip | configure | skip | skip | skip | openclaw.py:116-120 |
| openclaw | multi-model:litellm | configure | skip | skip | configure | skip | skip | skip | openclaw.py:116-120 |
| openclaw | multi-model:openrouter | configure | skip | skip | configure | skip | skip | skip | openclaw.py:116-120 |
| agy | any | report-only: used only through OpenClaw's Antigravity engine | | | | | | | detection.py:170-192; cockpits/__init__.py:23-35 |
| claude-desktop | any | report-only: not managed by ai-resources | | | | | | | _shared.py:564 |

OpenClaw engines (any mode):

| Engine | anthropic | google | vertex | openai | ollama | deepseek | moonshot | Source |
|---|---|---|---|---|---|---|---|---|
| claude-code | configure | skip | skip | skip | skip | skip | skip | openclaw.py:116-120 |
| codex | skip | skip | skip | configure | skip | skip | skip | openclaw.py:121-125 |
| antigravity | configure | configure | skip | configure | skip | skip | skip | openclaw.py:97-115, 1019-1026; model_pins.py:33-38 |
| native | skip | configure (unverified) | skip | skip | skip | skip | skip | openclaw.py:97-133, 296-344 |
<!-- compat-matrix:end -->

Notes:

- `agy` (Antigravity) is used only through OpenClaw's Antigravity engine, with
  agy's own model ids (for example `gemini-3.8-flash-low`), which differ from the
  API ids; the kit never maps one to the other. It needs agy installed and the
  unrestricted-execution risk acknowledged, and is never the default.
  Today agy is a chat-orchestrator engine and a report-only model surface; it is not a
  backend for kit roles. A workflow step can name another OpenClaw agent
  (`backend: openclaw-agent`), and an agy-backed agent would be one such agent, but
  an OpenClaw agent on `agy-cli` that takes role work through `sessions_spawn` is an
  external prerequisite that has not been shown to exist: see
  [ADR-0005](decisions/0005-cross-provider-role-dispatch.md).
- Claude Desktop is not managed by ai-resources.
- Aider under OpenRouter uses OpenRouter's OpenAI-compatible base
  `https://openrouter.ai/api/v1` (the Anthropic-shaped `https://openrouter.ai/api`
  is for Claude Code). An existing `~/.aider.conf.yml` that changes is kept next
  to the new one as `~/.aider.conf.yml.kit-bak`.
- A single non-Claude model keeps `mode: single-model` and needs no backend:
  Claude Code then keeps its own Claude models and no endpoint is written.

Non-interactive selection (`ai-resources setup --non-interactive`) takes
`--shape`, `--providers`, `--models`, `--smoke-path`, `--backend`, `--cockpits`
and `--allow-unverified`, prints the same plan, never prompts, and exits non-zero
with nothing written on a contradiction (for example `--shape single` with two
models, or `--cockpits claude` for a model the matrix skips).

## Per-cockpit configuration

| Cockpit | File written | Notes |
|---|---|---|
| Claude Code | `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/agents/*.md` | 40 subagents: 15 roles + 25 personas, each with its `model:` |
| Gemini CLI | `~/.gemini/settings.json`, `~/.gemini/GEMINI.md` | Engram MCP configured |
| Cursor | `~/.cursor/AGENT_KIT.md`, `~/.cursor/mcp.json` | |
| Codex CLI | `~/.codex/AGENTS.md` | |
| Aider | `~/.aider/CONVENTIONS.md`, `~/.aider.conf.yml` | architect/editor/weak models from executors; the conf file only in multi-model, with the gateway's OpenAI-compatible base |
| Windsurf | `~/.codeium/windsurf/memories/global_rules.md` | |
| Continue.dev | `~/.continue/AGENT_KIT.md` | |
| Copilot | `~/.vscode/copilot-instructions.md` | |
| OpenCode | `~/.config/opencode/AGENT_KIT.md` | |
| OpenClaw | `openclaw.json` via `openclaw config patch`, workspace `AGENTS.md` | Engine picked in setup; see below |

## OpenClaw (chat bots)

OpenClaw fronts chat channels such as Telegram and hands every turn to an agent
runtime. When OpenClaw is installed, step 7 asks which engine runs its default
agent, offering only engines whose CLI is installed:

| Engine | OpenClaw runtime | What the bot gets |
|---|---|---|
| Antigravity CLI (agy) | `agy-cli` | agy (a personal Google account) as chat-facing orchestrator: talks, hears voice notes, finds the project, and hands code/team work to a `claude` worker agent running unrestricted Claude Code. **Unrestricted code execution — read the Security section below before enabling this.** |
| Claude Code | `claude-cli` | Everything the Claude Code cockpit installs: subagents, skills, hooks, `/kit-plan`, `/kit-implement`, the CLAUDE.md block |
| Codex CLI | `codex` | What the Codex cockpit installs |
| Gemini CLI | — | Not offered: Google retired CLI access for personal accounts on 2026-06-18 |
| Keep | — | Nothing changes; if the kit configured OpenClaw before, it offers to restore the original |

The chain for Claude Code is the point: pick it and the bot runs the same setup as
your terminal, so a Telegram message can plan, delegate to subagents and load
skills. The `direct` engine (OpenClaw's own runtime, wired to OpenRouter) was
removed from the kit; OpenRouter itself is still a supported multi-model backend
(see above), it is just no longer wired through OpenClaw's own runtime. A
`setup-state.yaml` saved with the old `direct` engine falls back to `keep` on the
next run, with a warning.

### antigravity: agy → OpenClaw → Claude Code, unrestricted

Picking `antigravity` wires the whole chain:

```
Telegram → OpenClaw → agy (agy-cli backend) → sessions_spawn → claude-kit backend (claude, --dangerously-skip-permissions)
```

- **agy is the orchestrator** for every chat topic: it replies directly to
  anything that is not code or team work, reads voice notes via its own
  `view_file` tool (the `agy` voice mode: no ffmpeg, no whisper), and
  discovers projects under `~/Development/**`.
- **`claude` is the worker agent**: a second OpenClaw agent running the kit's own
  `claude-kit` CLI backend (not the bundled `claude-cli` one Claude Code uses), so
  none of core's restrictions on the bundled backend
  (`--strict-mcp-config`, `--setting-sources user`) apply. It runs with
  `--dangerously-skip-permissions` and the user's full tooling, workspace
  `~/Development`, no `operator.admin`.
- **Delegation**: agy calls `sessions_spawn agentId=claude cwd=<project>
  thread=true` (directly, or through the kit's `/claude` and `/equipo` shortcut
  commands — see `openclaw-plugin/ai-resources/`), which keeps the reply bound to
  the Telegram thread it started from.
- **The kit plugin** (`openclaw-plugin/ai-resources/`, linked by setup with
  `openclaw plugins install --link`) registers both CLI backends and the shortcut
  commands. It also ships a small stdio MCP bridge (`bridge.py`) that agy is
  pointed at once (`agy mcp add openclaw …`), because agy has no per-run MCP flag;
  each turn the bridge reads the MCP endpoint OpenClaw wrote for that run and
  forwards to it.
- **Voice** is the transcriber's third mode, `agy`, alongside `cloud` (OpenRouter)
  and `local` (whisper.cpp): agy hears the raw `.ogg` itself, with a 45s timeout,
  one retry (~90s worst case) and a fixed `[voice note could not be understood]`
  marker on failure — OpenClaw always gets a non-empty reply, and the marker is
  relayed to the user as "please resend it or type it". It is the default mode when
  `agy` is installed, because it needs neither an OpenRouter key nor a local model.
  Three details are load-bearing and were each found by running a real note: `-p`
  must be the last flag (agy takes the next argv element as the prompt), the note's
  directory needs `--add-dir` plus `--dangerously-skip-permissions` (it is staged
  outside agy's workspace, and a denied read prints nothing and exits 0), and
  `--json-schema` must not be combined with audio (it hung for 124s).
- Setup offers to install `claude`, `agy` and `openclaw` themselves if missing
  (step 2), and to remove a superseded hand-rolled `~/.local/bin/openclaw-transcribe`.
  Both `claude` and `agy` logins stay interactive — the kit never automates them.

#### Antigravity's two weekly quota pools

Antigravity serves **two independent weekly quota pools**: one for Gemini models, one
shared by Claude and GPT models. `ai-resources setup` labels each antigravity model
choice with its pool, and offers Claude/GPT models (`claude-sonnet-4-6`,
`claude-opus-4-6-thinking`, `gpt-oss-120b-medium`) alongside the default Gemini ones —
so a Gemini pool at 0% is no longer a dead end.

The failure mode is deceptive if you don't know this: agy retries a 429
`RESOURCE_EXHAUSTED` five times with backoff (~93s total), then OpenClaw's stall
detector kills the turn — *"This turn was interrupted because it stopped making
progress"* — and respawns it, with no quota error ever shown. agy's own background
quota refresh is broken (`Singleflight refresh failed: You are not logged into
Antigravity`), so an on-demand check is the only reliable signal.

Run `ai-resources doctor` to see both pools' remaining percentage and reset time. A
healthy pool looks like:

```
[4] Cockpits
  ✓ Gemini Models: 81% remaining (resets 2026-09-24T21:01:59Z)
```

An exhausted one that's backing your applied model looks like:

```
[4] Cockpits
  ✗ Gemini Models: 0% remaining (resets 2026-09-24T15:45:01Z)
    Re-run `ai-resources setup` and pick a model from the "Claude and GPT models" pool.
    Voice notes on agy share this same Gemini pool — consider switching voice notes off agy too.
```

Voice notes transcribed through `agy` are pinned to `gemini-3.8-flash-low`
(`voice/openclaw_transcribe.py`) — no Claude/GPT model accepts audio, so agy voice can
never leave the Gemini pool. If the main agent also runs a gemini-* model, the two
exhaust together; setup warns about this when you pick `agy` for voice, but does not
change the default (agy-first) for you.

**Known limitation, documented and not detected:** Google Workspace accounts
(`hd=<domain>`, token `auth_method=consumer`) cannot hold Google AI Pro/Ultra and
silently sit on the FREE weekly tier — this is not visible in any token claim the kit
can read, so it is not something `ai-resources doctor` can flag; the quota pools above
are what you can check.

Whatever the engine, Engram is declared in OpenClaw's `mcp.servers`, and the
workspace `AGENTS.md` names Engram as the memory of record, with OpenClaw's
native memory kept for recent chat context.

### MCP servers

Because of `--strict-mcp-config`, the bot sees only the servers in OpenClaw's
`mcp.servers`, never the ones you use in Claude Code. With the Claude Code engine,
step 7 lists Claude Code's user-level servers and asks **Give the bot the same MCP
servers as Claude Code?**, then lets you untick any of them. It reads:

| Source | What is mirrored |
|---|---|
| `~/.claude.json` `mcpServers` (user scope) | stdio and HTTP servers; project scopes are skipped |
| `~/.claude/settings.json` `mcpServers` | same |
| Enabled plugins' `.mcp.json` | same, with `${CLAUDE_PLUGIN_ROOT}` expanded |
| claude.ai connectors | only those with a public MCP endpoint (ClickUp → `https://mcp.clickup.com/mcp`); the rest are reported |

The preview shows each server's name, kind and action, never a value:

| Action | Meaning |
|---|---|
| add | Not in OpenClaw yet; the kit writes it |
| adopt | Already in OpenClaw exactly as the kit would write it; recorded as kit-managed, nothing written |
| update | Kit-managed and changed in Claude Code |
| remove | Kit-managed and gone from Claude Code; what it replaced is put back |
| skip | Defined differently in OpenClaw (or edited by hand after the kit wrote it), Engram (the engine step owns it), or not mirrorable |
| needs attention | Holds a literal credential; not mirrored |

**Credentials never reach `openclaw.json`.** `${VAR}` references are copied as they
are and OpenClaw resolves them from the gateway's environment. A literal value in a
secret-named env var (`*TOKEN*`, `*KEY*`, `*SECRET*`, …), a `KEY=value` argument such
as `docker -e GRAFANA_SERVICE_ACCOUNT_TOKEN=…`, a value after `--token`, a string with
a known token prefix (`glsa_`, `sk-`, `ghp_`, `sbp_`, …), an `Authorization` header or a
secret URL parameter marks the server as needing attention. The message names the
variable to use: for the Docker case, pass `-e GRAFANA_SERVICE_ACCOUNT_TOKEN` with no
value and set `"env": {"GRAFANA_SERVICE_ACCOUNT_TOKEN": "${GRAFANA_SERVICE_ACCOUNT_TOKEN}"}`
in Claude Code's config. Harmless literals such as `LOG_LEVEL=debug` are copied.

After applying, setup names any referenced variable the gateway lacks (on Linux it
checks the running gateway's environment by name) and tells you where to put it:
`NAME=value` in `~/.openclaw/.env`, then `openclaw gateway restart`. HTTP servers get
`auth: oauth`, which needs a browser, so setup lists the commands to run once:

```
openclaw mcp login <name>
```

Writes go through one validated `openclaw config patch`, retried while OpenClaw's
state database settles. Teardown removes servers the kit added and restores the ones
it replaced; a server you edited by hand since is left as it is. An unattended run
with no saved answer changes nothing.

The kit never writes `openclaw.json` itself: every change goes through
`openclaw config patch`, which validates it. `channels` (Telegram allowlists,
topic-to-agent bindings) is never part of any patch the kit builds; a channel account is turned off only by running
`openclaw channels remove` after a per-change confirm (ADR-0010). The values a
patch replaces are saved in `setup-state.yaml` and restored exactly on teardown,
including the antigravity-only keys (`tools.media`, the openrouter/llama-cpp
plugin entries, per-agent `subagents.allowAgents`, the `claude` agent entry) and
unlinking the plugin, only if the kit itself linked it. An unattended run
(`--non-interactive`) with no saved answer keeps the current engine, and applying
`antigravity` needs an explicit, saved risk acknowledgement — with neither, the
patch is skipped and the reason is printed, so a live bot is never repointed or
armed with unrestricted execution by accident.

Switching the kit's mode carries the bot along: OpenClaw starts Claude Code with
your user settings, so single-model keeps it on your Claude subscription and
multi-model routes it through the gateway, subagent models included. OpenClaw
always passes the bare Anthropic ID (`--model claude-sonnet-5-5`); LiteLLM
registers those IDs, and under OpenRouter setup writes a `modelOverrides` map to
the catalogue IDs (Claude Code 2.1.200 or later). Under OpenRouter you still need
`claude /logout`, which moves the bot off the subscription too.

### Voice notes

Step 7 also asks how OpenClaw transcribes voice notes before the agent sees them:

| Answer | What happens |
|---|---|
| Cloud (needs `OPENROUTER_API_KEY`) | An audio-native model on OpenRouter hears the note; local whisper.cpp is the fallback |
| Local only | whisper.cpp on this machine; the audio never leaves it |
| Off | `tools.media.audio.enabled: false` |
| Keep | Nothing changes; if the kit configured voice notes before, it offers to restore the original |

Both transcribing answers register the kit's
`scripts/ai_resources/voice/openclaw_transcribe.py` as a `tools.media` CLI model, run
with the kit's own Python. It prints only the transcript, and the first step that
produces one wins:

1. **Cloud only:** `google/gemini-3.7-flash` gets the note as MP3 `input_audio` with the
   glossary in its prompt. It answers `[inaudible]` for noise, and that answer is passed
   on as is. A network error or 5xx is retried once; a 4xx is not.
2. **whisper.cpp** `large-v3-turbo` q5_0 with a forced language, temperature 0, beam 5,
   loudness normalisation and a short prompt, followed by a text-only correction pass:
   `google/gemini-3.5-flash-lite` on OpenRouter, else Claude Code (`claude -p --model haiku`)
   on your subscription. When you choose local, setup asks whether to keep this pass:
   "Correct with an LLM" sends only the transcript text, never the audio (about 1 s through
   OpenRouter or about 10 s through Claude Code, about $0.0001 a note). "Fully offline"
   adds `--no-correct` to the command, so nothing leaves the machine.
3. **The raw Whisper transcript**, so a note is never lost.

These choices were measured on real Telegram notes in Spanish mixed with English
technical terms. The audio model was the only engine that got notes like "me listes los
tópicos… ¿eres capaz de hacerlo?" right; Whisper heard "añadiste… eres capaz de acudir".
A cloud note takes 3–6 s and typically costs $0.0004–0.0014 (from $0.0001 for a
one-liner to $0.0024 for a 30-second note); Whisper plus correction takes about 5 s, and
the correction costs about $0.0001–0.0002. Whisper's VAD and longer prompts
both made results worse.

Setup installs `whisper-cpp` and `ffmpeg` with Homebrew when they are missing, and
downloads the Whisper model to `~/.local/share/whisper-models/`. The file is written
under a `.part` name and renamed only after its SHA-256 matches. With cloud, a local
engine that fails to install is only a warning. Setup records what it installed itself.
Teardown deletes the model files it downloaded. It offers to `brew uninstall` only the
formulas that setup installed, and never ones that were already there.

The glossary at `~/.config/openclaw-voice/glossary.txt` lists terms the transcriber
should spell right. Setup writes a default only when the file is missing, so your edits
are kept. Setup asks for the language and passes it on the command line. The default is
the system locale's language (`LC_ALL`, then `LANG`), or Spanish if the locale is unset,
C or POSIX. `auto` is available, but short clips can be misdetected: in testing,
"¿Puedes escucharme?" came out in Russian. You
can tune the rest through the environment: `VOICE_AUDIO_MODEL`, `VOICE_TEXT_MODEL`,
`VOICE_WHISPER_PROMPT`, `WHISPER_MODELS`, `WHISPER_MODEL`, `VOICE_GLOSSARY` and
`VOICE_LOG`. The log (`~/.cache/openclaw-voice.log`) records the route, timings and cost
of each note, never its words.

The rule that tells the agent to act on a transcript at once goes into the kit block in
the workspace `AGENTS.md`: don't ask to confirm, don't repeat the note back, don't
transcribe again, and ask only when the note is `[inaudible]` or empty. If `AGENTS.md`
already has a `## Voice notes` section of its own, an interactive run offers to move it
into the block and backs up the original first. Decline, or run unattended, and your
section stays while the block leaves the rule out, so it never appears twice. Teardown
puts back the `tools.media` the kit replaced and removes the block.

## Automatic model updates

`ai-resources models` keeps the models you chose current without waiting for a kit release and without
hand-editing `openclaw.json`. It works on **slots**: a slot is `<provider>:<family>` (for example
`anthropic:sonnet`, `google:gemini-flash`). The four Claude classes are the built-in `anthropic:` slots, so a
host with no recorded selection behaves exactly as before.

**Who owns what** (ADR-0002). `setup-state.yaml` (`selection`) owns WHAT you want: providers, slots, the
primary, the smoke path. The overlay `~/.config/ai-resources/model-pins.json` (schema 2) owns HOW each slot
moves: pins, per-slot answers, approvals, pending proposals, history. The wizard writes the selection;
only `ai-resources models ...` writes the overlay. A schema-1 overlay is migrated on first save and kept
once as `model-pins.json.v1.bak`.

**One declaration.** Claude pins are declared once in `scripts/ai_resources/model_pins.py` (`DEFAULTS`);
every other provider's naming, ordering and discovery sources live in an adapter under
`scripts/ai_resources/model_providers/`. The effective id of a slot is the highest of the kit default, the
selection's id and the overlay pin, unless the slot is frozen or its track is `fixed`.

**Discovery.** Only providers that are enabled and credentialed are asked, in one run with one time budget:
an OpenClaw catalog listing (`openclaw models list --all --json --provider <id>`), then the vendor's own
`/models` for OpenAI, DeepSeek and Moonshot **only if you opted in per provider in the wizard** (the host is
hard-coded, the key goes only in the Authorization header and is never printed), else a static report-only
list. Credentials are detected from the kit's `.env` and from OpenClaw's auth status, reading only provider
names, kinds and counts, never key material. A provider that fails shows `discovery failed`; the run exits 1
only when every provider it asked failed. Aliases, previews, dated snapshots and unknown families are
reported, never applied.

**The answer ladder** (per slot, first match wins): `exclude` glob or `frozen`, then `never` (this id, or the
whole family), then an `approve`d id, then the slot's remembered `answer`:

| Answer | Meaning |
|--------|---------|
| `ask` | wait for a human (the default for every non-Claude slot) |
| `always` | apply a newer **stable** id on its own when the smoke test passes and the price is **known and not higher**, within `max_bump` (`minor` keeps major jumps for a human; `any` follows the whole family) |
| `never` | never propose it again (this model only, or the whole family) |

Claude slots keep their v1 behaviour (`always`, `max_bump: minor`). Prices are never invented: exact
`audit.PRICES`, then your own `prices` in the overlay, then OpenRouter's public list for an OpenRouter
backend (cached 24 h). Decisions are `auto`, `approved`, `needs_approval`, `excluded`, `frozen`,
`suppressed`, `report` and `current` (the slot is up to date).

**On a terminal** `ai-resources models update` discovers once, shows a table per slot (current -> proposed,
provider, channel, price, smoke path, and the artifacts it would touch), and asks: *Update now / Not now /
Always update this family / Never* for one proposal; *Update all shown / Choose per model / Not now* for
several. Only Always and Never change your policy. The lock is released while you read and taken again to
apply the same discovery (it exits 73 if another run holds it; it aborts with no write if the policy changed
meanwhile). **Unattended** (the systemd timer, `--unattended`, `--non-interactive`, no TTY) it never asks: it
records pending proposals with a timestamp, applies only `always` slots that pass every guardrail, and exits
10 so the wrapper sends the notice with the exact `ai-resources models approve ...` command.
`--apply-proposals` approves and applies every pending proposal without a prompt; `--review` forces the
interactive listing on a terminal. `--slot <provider:family>` selects a slot (`--class` is the alias for the
Claude ones).

**What an update does.** Smoke-test the new id on the selection's smoke path (`claude -p` for Claude on the
Claude CLI; the LiteLLM gateway, OpenRouter or a direct vendor call otherwise; a slot with no probe waits for
a human), defer if agent turns are in flight, back up, then apply every affected artifact through one
ordered registry (`scripts/ai_resources/model_fanout.py`): `openclaw.json` through `openclaw config patch`,
the overlay and the files that embed the old id: `executors.yaml`, `litellm.yaml`, the Claude subagent
files and settings and `~/.aider.conf.yml`. A failure restores the artifacts already written, in reverse
order. The
gateway is restarted with `watchdog.off` held, `openclaw health` is polled, a new PID is required, the model
is smoke-tested again, and the inverse patch rolls everything back if any of it fails. A slot whose provider
has no verified OpenClaw runtime (Google today) is recorded and re-rendered but never repointed in
`openclaw.json`.

**What is re-rendered, and when.** Each of those files is rewritten only if the compatibility matrix configures
its cockpit for this host AND the file embeds an id of a slot that moved. Single-model Claude aliases
(`opus`, `sonnet`) embed no id and a `fixed`-track slot never moves, so neither is rewritten. The ids are
replaced as whole strings in every spelling the file uses (bare, `google/...`, OpenRouter's namespace and
dotted minor). Each file is backed up next to the `openclaw.json` backup before it is written; LiteLLM is
restarted once and health-checked (not for OpenRouter or a remote gateway), and if it does not come up, or
any later step fails, every file is put back byte for byte and the update exits 2. The renderers write
`os.environ/<VAR>` references only: no key value is written, and the key already in `~/.aider.conf.yml` is
left as it was. The result lists the re-rendered files (`rerendered` in `--json`; the Telegram notice prints
them too).

**Writes stay on `config patch`.** The command never opens `openclaw.json` for writing: `config patch`
validates the schema and the kit asserts that no patch touches `channels`. The file copy under
`~/.openclaw/backups/models-update/<timestamp>/` is forensics only. The allowlist keeps the old model key
so a rollback stays valid.

**Exit codes** (the same table in the CLI, the wrapper script and the runbook):

| Code | Meaning |
|------|---------|
| 0 | OK, no change, cooldown, `--check` or `--dry-run` with nothing applicable |
| 1 | Error (every provider failed, I/O, the policy changed during a prompt, unexpected) |
| 2 | Switched, failed health, rolled back successfully (also: an artifact failed and the earlier ones were restored) |
| 4 | Smoke or patch validation failed before any write |
| 6 | Rollback failed (critical) |
| 10 | Approval pending (nothing applicable) |
| 11 | `models check` only: a change is ready to apply |
| 73 | Locked by another run |
| 75 | Deferred (gateway busy, or restart deferred; resumed by the next run) |

Antigravity (agy) models are report-only: edit `model_pins.AGY_STATIC` by hand.

**Kit defaults move with a kit release, and an unfrozen host follows on its next render.** The kit defaults
(`model_pins.DEFAULTS`) are the Claude 5.5 family (`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`;
fable stays `claude-fable-5-1`). The effective id of a slot is the highest of the kit default, the selection's
id and the overlay pin, so a host that has not frozen a slot moves to 5.5 the next time something renders:
the OpenClaw `*` primary (through `@MODEL_SONNET@`), the LiteLLM Claude passthrough and every shipped profile.
A frozen pin always wins; to stay on the previous id, freeze the slot before updating the kit
(`ai-resources models pin <class> <id>` freezes a slot at an explicit id; `ai-resources models unpin <class>`
lets it follow again). The kit never writes your overlay on its own and never approves a price:

- A kit default is not a `models update` proposal: it is read by `model_pins.effective()` and needs no approval.
  The answer ladder (`approve`, `always`, `max_bump`, the price guardrail) governs ids NEWER than the effective
  one. Haiku 4.5 to 5.5 would have been a **major** bump waiting for `ai-resources models approve haiku <id>`
  through `models update`; as a kit default it is not. Any later id still goes through the ladder.
- The catalogue lists the 5.5 ids but carries no price for them, and `audit.PRICES` has no row for them: the kit
  never invents one. Until you add a price (`prices` in the overlay) or a row lands, `ai-resources audit`
  shows `n/a` as the cost of those models (they are no longer priced as their major version).
- Under OpenRouter, `modelOverrides` maps the current generation only; a host frozen on a previous id needs the
  freeze removed or its own override.

## Tools the setup can install

Step 1 of `ai-resources setup` scans the AI CLIs and shows them as one list (the scan and the selector are the same
list): installed ones carry their version and cannot be ticked, missing ones show their exact install command and are
selectable (nothing is pre-ticked; Enter with nothing ticked skips and installs nothing), and Windsurf is listed as
`manual install: <url>` because the vendor documents no CLI installer. Install needs the tick in the same run; nothing
is stored or replayed. The plain scan list is printed only where no list can be shown (`--yes`, `--non-interactive`,
no terminal, a dry run without a terminal). After an install, a separate `Sign in now?` screen covers only the tools
installed in this run (stdio inherited, nothing captured). A login subcommand (`codex login`) is recorded as signed
in on exit 0; a bare `claude` or `agy` is recorded as "started sign-in" because the operator finishes inside the
tool. The legacy `gh copilot` extension is shown as `gh copilot extension` and signs in through `gh auth login`; the
Cursor login runs only the `agent` binary beside the detected `cursor-agent`. The kit never asks for or stores an
API key. `setup --dry-run` on a terminal previews this list and prints what it would run.
`--yes` installs only the missing `claude`, `agy` and `openclaw`.

Install and sign-in commands were checked against the vendor's documentation on 2026-10-16 (the same URL and date are
recorded in `scripts/ai_resources/setup/tools.py`, and a test fails if one is missing):

| Tool | Install | Sign-in | Documentation |
|---|---|---|---|
| Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash` | `claude` | https://code.claude.com/docs/en/setup |
| Antigravity CLI | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | `agy` | https://antigravity.google/docs/getting-started?tab=cli |
| OpenClaw | `curl -fsSL https://openclaw.ai/install.sh \| bash -s -- --no-onboard --no-prompt` | none | https://docs.openclaw.ai/install |
| Cursor Agent CLI | `curl https://cursor.com/install -fsS \| bash` | `agent login` | https://cursor.com/docs/cli/installation |
| Codex CLI | `npm install -g @openai/codex` | `codex login` | https://github.com/openai/codex#readme |
| Aider | `curl -LsSf https://aider.chat/install.sh \| sh` | none: export the provider key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ...) | https://aider.chat/docs/install.html |
| GitHub Copilot CLI | `npm install -g @github/copilot` | `copilot`, then `/login` | https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli |
| Continue.dev CLI | `npm i -g @continuedev/cli` | `cn login` | https://github.com/continuedev/continue/blob/main/extensions/cli/README.md |
| OpenCode | `npm install -g opencode-ai` | `opencode auth login` | https://opencode.ai/docs/ |
| Windsurf (now Devin Desktop) | manual (desktop IDE) | n/a | https://docs.devin.ai/desktop/getting-started |

**Gemini CLI is not installable.** Google retired it for personal accounts on 2026-06-18 and replaced it with the
Antigravity CLI (`agy`; migration guide https://antigravity.google/docs/cli/gcli-migration). A copy that is already
installed appears in the list as an informational row (`Gemini CLI vX (retired for personal accounts; use agy)`) and is
never offered for installation. The per-installer audit evidence is in ADR-0008, item 14.

## Operations

```sh
ai-resources doctor            # full health check, both backends
ai-resources audit             # cost report

# LiteLLM backend only — under OpenRouter these report that there is nothing to manage
ai-resources daemon status
ai-resources daemon start
ai-resources daemon stop
ai-resources daemon restart
ai-resources daemon logs
ai-resources daemon update
```

## Cost reporting is wrong under OpenRouter

Claude Code computes every figure it shows — `/usage`, the status line,
`--max-budget-usd` — locally, from token counts at **Anthropic list prices**.
Route through OpenRouter and those numbers describe a bill nobody is sending
you. A run costing cents can report dollars.

The spend limit on the OpenRouter key is the real ceiling, and
<https://openrouter.ai/activity> is the real breakdown. Set a limit per key at
<https://openrouter.ai/settings/keys>.

The LiteLLM backend has the same problem for non-Anthropic routes; there,
`ai-resources audit` reads the gateway's own logs instead.

## Troubleshooting

**Every request fails under OpenRouter, and Claude Code is logged in.**
OpenRouter rejects a request carrying both a bearer token and an OAuth login.
Run `claude /logout`, open a new terminal, then `ai-resources doctor`.

**`[claude-code:unrecognized_model]` on every start.**
Expected noise, not a failure. Claude Code does not recognise namespaced IDs
such as `anthropic/claude-sonnet-5.5`, warns, and sends them anyway.

**`claude --model claude-sonnet-5-5` fails under OpenRouter, or a bot on the
claude-cli runtime cannot answer.**
OpenRouter rejects bare Anthropic IDs. Setup maps them with `modelOverrides` in
`~/.claude/settings.json`; re-run `ai-resources setup` if the map is missing, and
check `claude --version` is 2.1.200 or later — older versions ignore the map for
`--model`.

**The main conversation fails but subagents work (OpenRouter).**
The profile has no `classes` block, so `/model` aliases resolve to bare Claude
IDs the catalogue does not know. Add one, or pick a shipped openrouter profile.

**A model ID is rejected.**
OpenRouter IDs are namespaced and exact: `anthropic/claude-haiku-5.5` exists,
`anthropic/claude-haiku-5-5` does not. Check the live catalogue rather than
guessing. On the LiteLLM backend, check that `litellm.yaml` lists the model and
the provider key is in `.env`. Either way, `ai-resources executors test <name>`
isolates it.

**Gateway won't start (LiteLLM).**
```sh
ai-resources daemon logs --tail 200
docker ps -a | grep ai-resources-litellm
```

**Nothing current works on Vertex.**
Regional endpoints such as `us-east5` serve Claude Sonnet 4.6 and earlier only.
Set `GOOGLE_CLOUD_LOCATION=global`, which is also the tier without the 10%
premium that multi-region and regional endpoints carry. The `vertex` provider is
still selectable in the wizard; only the prebuilt `vertex-enterprise` profile was
retired, so a Vertex setup is now assembled per role rather than picked as a
preset.

**Prompt caching seems off.**
Caching survives when routing to Claude and is lost when routing elsewhere, so a
cheaper model can cost more per completed task. Check that
`cache_read_input_tokens` appears on Claude calls.

**Tool calls fail on a non-Claude model.**
Most translate cleanly. Known edges: parallel tool calls may be serialised, and
complex JSON Schema with `$ref` may be flattened. Not every model supports tool
use at all — on OpenRouter, check `supported_parameters` before assigning one to
a role. Workaround: put that role back on Claude.

**Engram MCP not visible to a non-Claude subagent.**
Ensure the cockpit has Engram in its MCP config; verify with `ai-resources
doctor`.

## See also

- `rules/017-multimodel-routing.mdc` — kit-internal rule
- `docs/litellm-service.md` — gateway operations
- <https://openrouter.ai/docs> — OpenRouter docs
- <https://docs.litellm.ai/> — upstream LiteLLM docs
