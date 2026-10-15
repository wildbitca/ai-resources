# ADR-0008: The setup wizard stays on rich and questionary, with step-level Back and a Ctrl-C that exits 130

**Status**: Accepted (implemented on `main`, not released; the label is the operator's decision at release time)
**Date**: 2026-10-15
**Decision makers**: planner, implementer, operator (decisions recorded in `.agent-output/planner/setup-wizard-tui-plan.md`)
**Amends**: none. [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md) and [ADR-0007](0007-subscription-lanes.md) are unchanged: no restart gate, backup or acknowledgement rule moves.

## Context

`ai-resources setup` asked 20 questions in a row with a bare `[n/10]` header. Four defects came from one cause, questionary's `Question.ask()`, which swallows `KeyboardInterrupt` and returns `None`: Ctrl-C at the resume confirm read as "No", at the port prompt crashed on `int(None)`, at the profile confirm read as "decline", and at the lanes acknowledgement ended the run with exit 0. There was no way to go back, no review of the whole answer set, no validation while typing and no useful output when an installer failed.

## Decision

1. **No new dependency and no full-screen app.** rich 13.9.4, questionary 2.1.0 and prompt_toolkit 3.0.48 stay as pinned in `Formula/ai-resources.rb`. Textual, curses and whiptail are out. A live rich region fights prompt_toolkit for the terminal while a prompt is open, so the progress rail is printed once per step header instead.
2. **Ctrl-C and Ctrl-D are scoped to the wizard and exit 130.** Inside `ui.prompt_scope(wizard=True)` the five prompt wrappers use `unsafe_ask()` and raise `Cancelled` on `KeyboardInterrupt` or `EOFError`. Outside the scope they behave exactly as before (`None`), so doctor, executors, models and the cockpit commands are unchanged. `wizard.run` reports the cancel from inside the scope, names the step and lists the real side effects recorded so far (`ui.record_action` at every install, state save and write). `cli.main` is a backstop for a stray `KeyboardInterrupt`. The lock is released because `locked_run` is a context manager.
3. **A step registry, not a rewrite.** `setup/flow.py` is stdlib only. `wizard._run` keeps its preamble (dependency check, non-TTY guard before any banner, banner, corrupt-state handling, resume question, preflights) and then runs `flow.Wizard` over a list of `StepSpec` that call the existing step functions in the existing order, with the existing skip wording.
4. **Back is step-level and refuses to cross a change.** Ctrl-B is offered only in steps without side effects (models, role assignment, lanes, cockpit selection). The binding is merged into the built question's `application.key_bindings` (select and checkbox reject a `key_bindings=` kwarg) and exits with a `BACK` sentinel that the wrapper turns into `GoBack`. The runner restores the in-memory state snapshot taken before the aborted step and re-asks the previous step with its answers as defaults, but only when that step is rewindable and recorded no action. Otherwise it prints a notice and asks the same step again. Tool installs, the gateway install and the mid-flow state save, apply and verify are never rewound; the gateway is rewindable in `--dry-run` only.
5. **The review is the way to change an earlier answer.** Step 9 shows one row per section from the setup state, with credentials only as `set` or `missing`, and its select gains `Edit a section...` next to Apply, Change models and Cancel. Picking a section raises `JumpTo`; the run continues linearly from that step with the current answers as defaults and returns to the review. A host with no recorded model selection keeps today's silent skip.
6. **Numbering stays at 10.** Models, roles and lanes are sub-steps 6a, 6b and 6c, so the existing assertions on `TOTAL_STEPS` and `(10, 10, "Verification")` stay valid.
7. **Terminal behaviour.** `NO_COLOR` wins over `FORCE_COLOR` and strips colour only. Under `NO_COLOR` or a non-UTF-8 stream the glyphs are ASCII. The rail is full from 80 columns, wrapped from 60, a single `Step n/10  title` line below 60, and the plain header only in a non-interactive run. Without a TTY the wizard still fails with the existing error before any banner or rail.
8. **Output of failures and dry runs is masked.** `ui.mask_secrets` hides key-like `NAME=value` pairs, well-known token shapes, Bearer tokens and long opaque strings. A failing command prints its last 20 lines through it; the dry run prints a unified diff per renderable target, `new file` when it is missing.

9. **Echoed outside text is cleaned and masked.** Text that comes from a subprocess, a remote or a file passes `ui.clean_text` (ANSI/OSC sequences and control characters dropped, rich markup escaped by every message sink) and `ui.safe_text` (clean, then `mask_secrets`). `mask_secrets` also covers an unquoted `NAME=value with spaces` line for PASSWORD, PASSWD and PASSPHRASE names (masked to the end of the line), `Authorization:` headers, `-u user:pass`, YAML block scalars, PEM private keys and padded base64. The dry-run diff masks the whole text, not line by line.
10. **A cancel inside a cockpit configurator is recorded.** The configurator writes several files in turn, so an interrupt is recorded as `may be partially written: <cockpit>` and the cancel block lists it. A cancel at the review prints the same block as every other cancel point.
11. **A re-run offers every saved answer as its default.** The model picker (how, shape, provider, models, primary) and the checkboxes (a questionary checkbox reads `checked` per choice, so the default list is applied to the choices) start from the saved selection.

## Consequences

- Ctrl-C at the lanes acknowledgement now exits 130 instead of 0. This is the fix, recorded under Fixed in the changelog.
- Keys: Ctrl-B replaces cursor-left in text and password prompts of the steps that offer Back.
- The real key path (a pipe input driving questionary) is tested only where questionary is installed; CI installs only pyyaml, pytest and httpx, so those cases skip there and doubles cover the rest. A throwaway venv with the pinned questionary, prompt_toolkit and rich runs them (the 2026-10 real-library run found the checkbox default being dropped).
- The wizard's other prompts (doctor, executors, models, cockpits) still return `None` on Ctrl-C. Moving them to the scope is a follow-up.

## Rollback

Each change is its own commit. Reverting the runner, the header, Back, the review, the validators or the log tail and diff leaves the Ctrl-C fix in place. There is no state schema change and no migration.
