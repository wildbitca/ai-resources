# Run ledger: measured, adaptive sizing

Read this only when a workflow step tells you to (`/kit-plan` classifies, `/kit-implement` reassesses and closes) or the owner asks for the calibration report. It adds measurement to "Task size"; it changes no rule by itself.

## What is recorded

One line per finished run in `~/.local/state/ai-resources/run-ledger.jsonl` (append-only, never under `/tmp`): `mode` (final), `estimated` (the recommendation at the start), `forced`, `kind`, `risks`, `escalations`, `agents`, `steps`, `rework` (times the verifier sent work back), `outcome` (`pass|gaps|abandoned`), `lines`, `units`, `files`, `wall_s`, and `input`/`output`/`usd`/`calls` read from the same transcripts as `ai-resources audit` for the run window.

Tokens and USD are API-equivalent and an upper bound: the window also catches any other session running at that time. Pass `--session <id>` (repeatable) when the session ids are known to price exactly those. Not measurable without touching OpenClaw core: tokens per step of an inline run, and the fixed prefix of a child that reported no session id. Engram has no Python reader, so it is not a source.

## Dispatch ledger

Per-dispatch rows (role, pool, backend, model, family, tokens, duration, outcome, fallback cause, run id, writer families) live in `dispatches.jsonl` next to this ledger, written by `ai-resources route --record`, `agy run`, the orchestrator and `ai-resources dispatch record|list [--run R] [--since 2h]`. `runs record --from-dispatches RUN` fills `steps`, `agents`, tokens and `rework` from those rows instead of by hand; doctor compares them with the matrix (role ran outside its primary without a recorded cause is an alert). This file stays the per-run sizing record.

## Commands

| Command | Use |
|---|---|
| `ai-resources runs suggest --kind K --files N --lines F --units U [--risk 3,5] [--ambiguous] [--resume-fail] [--separate] [--force MODE]` | Recommend `inline`, `light` or `full`, with `basis` (`heuristic`, `history`, `forced`) and the number of similar runs |
| `ai-resources runs reassess MODE [--verifier-fail] [--files-estimated N --files-actual M] [--sensitive] [--trivial]` | Mid-run advice; each signal moves one step, any direction, no floor or ceiling |
| `ai-resources runs record --mode M --outcome O --agents A --steps S --rework R ... --start EPOCH` | Append one record (`--from-run ID` reads them from an orchestrator run, `--from-dispatches RUN` from the dispatch ledger) |
| `ai-resources runs list` / `report [--json]` | Last records / cost against quality per mode, with proposals |

## Rules

1. Heuristic first. History (same `kind`, same risk flag) moves the mode one step only with 5 or more similar runs: up when the heuristic mode needed rework in 40% or more of them, down when the lower mode passed with under 20% rework.
2. Recommend, never impose. The owner's `--inline`, `--light` or `--full` wins and is recorded as `forced`; you may depart from a recommendation with a stated reason.
3. Escalate or step down mid-run on signals: a verifier GAPS, more than twice the estimated files, a sensitive area appearing (inline goes to light), a trivial full task. Record each move in `escalations`.
4. Calibration proposals are text. A threshold change (the 5-run minimum, 40%, the 100-line overkill mark) happens only after the owner approves it, and the report prints the sample size beside every number; below 5 runs of a mode it proposes nothing.
