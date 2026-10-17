# ADR-0008: The setup wizard stays on rich and questionary, with step-level Back and a Ctrl-C that exits 130

**Status**: Accepted (implemented on `main`, not released; the label is the operator's decision at release time)
**Date**: 2026-10-15
**Decision makers**: planner, implementer, operator (decisions recorded in `.agent-output/planner/setup-wizard-tui-plan.md`)
**Amends**: none. [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md) and [ADR-0007](0007-subscription-lanes.md) are unchanged: no restart gate, backup or acknowledgement rule moves.

## Context

`ai-resources setup` asked 20 questions in a row with a bare `[n/10]` header. Four defects came from one cause, questionary's `Question.ask()`, which swallows `KeyboardInterrupt` and returns `None`: Ctrl-C at the resume confirm read as "No", at the port prompt crashed on `int(None)`, at the profile confirm read as "decline", and at the lanes acknowledgement ended the run with exit 0. There was no way to go back, no review of the whole answer set, no validation while typing and no useful output when an installer failed.

## Decision

1. **No new dependency and no full-screen app.** rich 13.9.4, questionary 2.1.0 and prompt_toolkit 3.0.48 stay as pinned in `Formula/ai-resources.rb`; CI runs the tests against those versions and against the newest ones (rich 15.0.0, questionary 2.1.1, prompt_toolkit 3.0.53) in a matrix. Textual, curses and whiptail are out. A live rich region fights prompt_toolkit for the terminal while a prompt is open, so the progress rail is printed once per step header instead.
2. **Ctrl-C and Ctrl-D are scoped to the wizard and exit 130.** Inside `ui.prompt_scope(wizard=True)` the five prompt wrappers use `unsafe_ask()` and raise `Cancelled` on `KeyboardInterrupt` or `EOFError`. questionary's select and checkbox do not turn Ctrl-D into EOF, so an eager `c-d` binding is merged into every question's Application together with Ctrl-B; it exits with a cancel sentinel (in a text field that holds text it keeps deleting the character under the cursor). Outside the scope they behave exactly as before (`None`), so doctor, executors, models and the cockpit commands are unchanged. `wizard.run` reports the cancel from inside the scope, names the step and lists the real side effects recorded so far (`ui.record_action` at every install, state save and write). `cli.main` is a backstop for a stray `KeyboardInterrupt`. The lock is released because `locked_run` is a context manager.
3. **A step registry, not a rewrite.** `setup/flow.py` is stdlib only. `wizard._run` keeps its preamble (dependency check, non-TTY guard before any banner, banner, corrupt-state handling, resume question, preflights) and then runs `flow.Wizard` over a list of `StepSpec` that call the existing step functions in the existing order, with the existing skip wording.
4. **Back is step-level and refuses to cross a change.** Ctrl-B is offered only in steps without side effects (models, role assignment, lanes, cockpit selection). The binding is merged into the built question's `application.key_bindings` (select and checkbox reject a `key_bindings=` kwarg) and exits with a `BACK` sentinel that the wrapper turns into `GoBack`. The runner restores the in-memory state snapshot taken before the aborted step and re-asks the previous step with its answers as defaults, but only when that step is rewindable and recorded no action. Otherwise it prints a notice and asks the same step again. Tool installs, the gateway install and the mid-flow state save, apply and verify are never rewound; the gateway is rewindable in `--dry-run` only.
5. **The review is the way to change an earlier answer.** Step 9 shows one row per section from the setup state, with credentials only as `set` or `missing`, and its select gains `Edit a section...` next to Apply, Change models and Cancel. Picking a section raises `JumpTo`; the run continues linearly from that step with the current answers as defaults and returns to the review. The review is shown on the kit-profile path too (a host with no recorded model selection gets the answer rows and the Apply question, without the per-cockpit plan); a non-interactive run keeps its silent skip because nothing can answer. Review writes nothing of the kit's configuration, but earlier steps already installed tools, saved credentials or the gateway state when confirmed; the cancel block lists them.
6. **Numbering stays at 10.** Models, roles and lanes are sub-steps 6a, 6b and 6c, so the existing assertions on `TOTAL_STEPS` and `(10, 10, "Verification")` stay valid.
7. **Terminal behaviour.** `NO_COLOR` wins over `FORCE_COLOR` and strips colour only. Under `NO_COLOR` or a non-UTF-8 stream the glyphs are ASCII. The rail is full from 80 columns, wrapped from 60, a single `Step n/10  title` line below 60, and the plain header only in a non-interactive run. Without a TTY the wizard still fails with the existing error before any banner or rail.
8. **Output of failures and dry runs is masked.** `ui.mask_secrets` hides key-like `NAME=value` pairs, well-known token shapes, Bearer tokens and long opaque strings. A failing command prints its last 20 lines through it; the dry run prints a unified diff per renderable target, `new file` when it is missing.

9. **Echoed outside text is cleaned and masked.** Text that comes from a subprocess, a remote or a file passes `ui.clean_text` (ANSI/OSC sequences and control characters dropped, rich markup escaped by every message sink) and `ui.safe_text` (clean, then `mask_secrets`). `mask_secrets` also covers an unquoted `NAME=value with spaces` line for PASSWORD, PASSWD and PASSPHRASE names (masked to the end of the line), `Authorization:` headers, `-u user:pass`, YAML block scalars, PEM private keys and padded base64. The dry-run diff masks the whole text, not line by line.
10. **A cancel inside a cockpit configurator is recorded.** The configurator writes several files in turn, so an interrupt is recorded as `may be partially written: <cockpit>` and the cancel block lists it. A cancel at the review prints the same block as every other cancel point.
11. **A re-run offers every saved answer as its default.** The model picker (how, shape, provider, models, primary) and the checkboxes (a questionary checkbox reads `checked` per choice, so the default list is applied to the choices) start from the saved selection.
12. **Step 1 tool selector and sign-in.** The tools offer is a checkbox over every known tool (installed rows disabled, missing rows unticked so Enter installs nothing, none allowed, plus a `Sign in to installed tools` row per installed tool with a login) instead of one yes/no, and newly installed tools with a login flow get a `Sign in now?` follow-up that runs the tool's own login on the terminal with inherited stdio. The install still happens in this step, so Back stays refused after it. `--yes`, `--dry-run`, `--non-interactive` and no-TTY keep their behaviour; `--yes` prints login commands and never runs a sign-in.
13. **Step 1 selector covers every cockpit the scan detects.** `tools.TOOLS` now has one entry per detectable cockpit with an official installer (Cursor Agent CLI, Gemini CLI, Codex CLI, Aider, GitHub Copilot CLI, Continue.dev CLI, OpenCode, plus the original Claude Code, agy and OpenClaw); a product without one (Windsurf) is a disabled `manual install: <url>` row that is never executed. Each entry records the vendor doc URL and the date its commands were checked, and a test fails when one is missing. The offer runs before the "no cockpits detected" stop. Nothing is pre-ticked unless the caller passes `preselect`. `setup --dry-run` on a terminal previews the selector and prints what it would run (the dry-run contract of installing nothing is unchanged; only the "asks nothing" part is widened, and only on a terminal without `--yes`). **Amended (unified list):** the detection scan and the selector are ONE list. Installed tools show with their version and are not selectable, installable ones are tickable, and a manual-install product (Windsurf) is an informational row with its URL; the plain scan list is printed only where no selector is shown (`--yes`, `--non-interactive`, no terminal, dry run without a terminal, nothing missing). Skip is not a row: Enter with nothing ticked installs nothing, and the prompt says so. Sign-in left the selector: it is a separate screen after the install, only for the tools installed in this run that have a login flow (pre-ticked), and already installed tools get one dim line with the command to run later. A bare `claude`/`agy` sign-in is recorded as "started", not "signed in" (exit 0 proves nothing); the Cursor login never resolves a bare `agent` through PATH; the legacy `gh copilot` extension is shown distinctly and signs in through `gh`. The dry-run preview answer and the sign-in answer are asked once per run.
14. **Installer audit and Gemini CLI retirement (2026-10-16).** Detection resolves every binary through one lookup (PATH, then Homebrew, `~/.local/bin`, npm/pnpm/bun/cargo global bins, fnm) and probes the version by absolute path, so `setup` no longer reports an installed tool as missing when it runs under a minimal PATH; a binary that exists but fails `--version` is "installed, version unknown". Gemini CLI left `tools.TOOLS` (Google retired it for personal accounts on 2026-06-18; Antigravity CLI replaces it): an existing copy is only an informational row. Every remaining installer was checked read-only against the vendor's current docs (`npm view`, HTTP GET); nothing was installed or signed in. Evidence:

| Tool | Command in the kit | Evidence (2026-10-16) | Result |
|---|---|---|---|
| Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash` | code.claude.com/docs/en/setup lists it as the recommended native install; URL 200 (redirects to downloads.claude.ai bootstrap.sh); `npm view @anthropic-ai/claude-code`: 2.1.301, bin `claude`, not deprecated; plain `claude` signs in | unchanged |
| Antigravity CLI | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | script 200, installs `~/.local/bin/agy`; docs page is client-rendered, so the command is verified from the script, not the page (NOT VERIFIED against prose docs) | unchanged |
| OpenClaw | `curl -fsSL https://openclaw.ai/install.sh \| bash -s -- --no-onboard --no-prompt` | docs.openclaw.ai/install: "Recommended: installer script", `--no-onboard` documented; `npm view openclaw`: 2026.10.4, bin `openclaw`; the npm route now needs `--allow-scripts=openclaw` on npm 11.16+/12 | changed from `npm i -g openclaw` |
| Cursor Agent CLI | `curl https://cursor.com/install -fsS \| bash` | cursor.com/docs/cli/installation shows this exact command; `agent login` and `CURSOR_API_KEY` in the authentication doc | unchanged |
| Codex CLI | `npm install -g @openai/codex` | README documents npm (also brew cask, and `chatgpt.com/codex/install.sh`, 200); `npm view`: 0.163.0, bin `codex`; `codex login` in developers.openai.com/codex/auth | unchanged |
| Aider | `curl -LsSf https://aider.chat/install.sh \| sh` | aider.chat/docs/install.html one-liner "installs aider, along with python 3.12 if needed"; PyPI `aider-chat` 0.86.2 requires Python `<3.13,>=3.10`, so the old `pipx install aider-chat` fails on this host (Python 3.15) | changed from `pipx install aider-chat` |
| GitHub Copilot CLI | `npm install -g @github/copilot` | docs.github.com install page lists it (Node 22+); `npm view`: 1.0.98, bin `copilot`; `/login` documented. `github/gh-copilot` is archived, so the "not installed" hints no longer suggest it | hints fixed |
| Continue.dev CLI | `npm i -g @continuedev/cli` | extensions/cli README lists it (Node 20+) and `cn login`; `npm view`: 1.5.47, bin `cn`; `docs.continue.dev/getting-started` is 404, so the detection hint now names the npm command | hint fixed |
| OpenCode | `npm install -g opencode-ai` | opencode.ai/docs lists npm (the `curl -fsSL https://opencode.ai/install \| bash` one is listed first; both valid); `npm view`: 1.19.3, bin `opencode`; `opencode auth login` documented | unchanged |
| Windsurf | manual | docs.windsurf.com redirects to docs.devin.ai/desktop/getting-started: the product was renamed Devin Desktop; no CLI installer documented. Detection still looks for `Windsurf.app` | URL updated |
| Gemini CLI | removed | `npm view @google/gemini-cli` still resolves (0.64.0) but Google retired it for personal accounts; migration doc antigravity.google/docs/cli/gcli-migration (200) | removed from the catalog |

## Consequences

- Ctrl-C at the lanes acknowledgement now exits 130 instead of 0. This is the fix, recorded under Fixed in the changelog.
- Keys: Ctrl-B replaces cursor-left in text and password prompts of the steps that offer Back.
- The real key path (a pipe input driving questionary) is tested only where questionary is installed; CI installs the Formula pins and the newest releases (matrix), so those cases run there; doubles cover the rest. The 2026-10 real-library run found the checkbox default being dropped.
- The wizard's other prompts (doctor, executors, models, cockpits) still return `None` on Ctrl-C. Moving them to the scope is a follow-up.

## Addendum: mode question with a CLI-subscriptions option

The mode question gains "multi-model via your CLI subscriptions" (second of four). Decision: the mode stays `single-model`
and an additive `cli_team` flag (absent from the file while false) names the team, rather than a new `backend: cli`
under `multi-model`. Reason: every gateway code path (steps 3, 4, 5, 8, smoke, executors rendering, doctor, rerender)
tests `mode == "multi-model"`, so a single-model-shaped state cannot reach any of them, and an old file keeps its
meaning with no migration. `setup --backend cli` is the flag spelling. Moving from a gateway mode runs the multi-model
cleanup offer; moving between this mode and single-model never touches gateway artifacts. Rollback: remove the option,
the flag and the key (an unknown `cli_team` key in a state file is ignored by older readers).

## Addendum: one preset question opens step 6

Step 6 asked how to choose models, then a provider, a shape, models and a primary, then a profile list of about eight
names and "Customize any role?". That is too much for the common case. It now opens with one question: Balanced
(recommended), Performance, Low pricing or Custom. Decisions:

- A preset is a kit profile chosen by mode and backend (`wizard.PRESET_PROFILES`); it never asks for a vendor or a
  model. The three profiles of a row differ, so a saved profile maps back to one preset. Only the profile name is
  stored: the state file has no new field, so old files load unchanged and a downgrade ignores nothing.
- Custom keeps the previous questions untouched. The preset is a new step `preset` before `models` and `profile`; when
  a preset answered, those two are skipped without output (empty skip reason), so Back lands on the preset question.
- Gateway rows use Claude-only profiles valid in every compatibility cell without `--allow-unverified`. The mapped
  `quality-first` and `cost-optimized` name Google models that the Claude cockpit has not verified, so
  `performance-claude-55` and `economy-claude-55` were added instead. OpenRouter keeps `openrouter-balanced` and
  `openrouter-cheap` and reuses `performance-claude-55` (namespaced ids, gateway-agnostic) for Performance.
- The no-gateway rows share `subscription-performance` and `subscription-economy`, both built on aliases with the
  verifier and security auditor on Opus and no writer on Opus. They carry a lane table, used only when lanes are on.
- The flags path (`--profile`, `--models`, `--shape`, `--providers`, `--compose`, `--non-interactive`) bypasses the
  question. A saved customized, composed or account-picked team defaults the question to Custom.
- The review's "Edit a section" offers one "Models" entry, which re-opens the preset question.

Rollback: remove the `preset` step and the four profiles; no state migration.

## Rollback

Each change is its own commit. Reverting the runner, the header, Back, the review, the validators or the log tail and diff leaves the Ctrl-C fix in place. There is no state schema change and no migration.
