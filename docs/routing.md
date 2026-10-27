# Routing and pool balancing

The decision is [ADR-0018](decisions/0018-routing-matrix-and-pool-balancing.md); this page is the operator view.

## The four pools

| Pool | What drains it |
|---|---|
| `claude-subscription` | Claude Code on Max: writers, planners, checkers |
| `agy-gemini`, `agy-claude-gpt` | Antigravity on AI Pro: explore, code review, doc, test and upgrade work, checker-ladder fallback |
| `anthropic-api` | OpenClaw chat agents (`main`, channel agents) and the Haiku fallback |
| `gemini-api` | utility, summarization, bulk explore on `google/gemini-3.8-flash` |

## The matrix

Source: `scripts/ai_resources/scoring/routing.py`. Rendered into `~/.config/ai-resources/lanes.yaml` under `routing:` by
`ai-resources setup`. Print it with `python3 -c "from ai_resources.scoring import routing; print('\n'.join(routing.table_lines()))"`
(run from `scripts/`). A profile overrides a cell with:

```yaml
routing:
  team:
    explore: { primary: "claude-cli:haiku" }
```

## Generated from the matrix

`scoring/routing.py` `ROLES` is the only table. These artifacts are generated from it (and from the profile's `routing:`
override) and `tests/test_matrix_equality.py` fails when one of them drifts:

| Artifact | Generator |
|---|---|
| `~/.config/ai-resources/lanes.yaml` (`routing.matrix`, digest) | `setup/lanes_render.py` |
| `agents/roles/*.md` marked `model:` block (Claude Code hosts the role on its first Max cell) | `ai-resources generate` (`generate._sync_role_models`) |
| OpenClaw `kit-role-*` agents: `model.primary` + `model.fallbacks`, Max cells pinned to the `claude-cli` runtime, agy cells as `agy-cli-lane/<slug>` | `setup/cockpits/openclaw_roles.py` |
| the setup role table | `routing.table_rows` |

Never edit a generated block by hand.

## Dispatch

Every workflow step resolves its role here before it runs (D6, ADR-0019):

| Command | Does |
|---|---|
| `ai-resources route <role> [--run R] [--record --step S] [--refresh] [--json]` | Resolve the role now: cell, pool, fallback chain, cause. `--run` reads the writer families of the run from the dispatch ledger (a checker takes the cheapest family that did not write); `--record` appends the decision |
| `ai-resources agy run --role <role> [--run R --step S] --prompt-file F` | Run an agy cell from a Claude Code session (no `sessions_spawn`); returns the report contract, exit 75 + cause + next cell on quota/timeout/empty/missing |
| `ai-resources dispatch record\|list [--run R] [--since 2h]` | The dispatch ledger (`dispatches.jsonl`): role, pool, backend, model, tokens, duration, outcome, fallback cause, writer families |

The fallback fires only on pool exhaustion or no response, always with a recorded cause; a route that cannot run is
`Blocked: <cause>`. `ai-resources doctor` compares the ledger with the matrix per pool and role. `ai-resources acceptance`
(per-role live calls and fault injection) is coming in P7 of 3.2.0.

## Balancing

`routing.choose_pool(class, usage)`: the first pool of the class under 85% used serves it; unknown counts as available; if all
are hot, the least used. `ai-resources doctor` prints each pool's usage and where every class would run now.

## Feeding the credit pools

Doctor reads `~/.cache/ai-resources/api-spend.json` (`{"anthropic-api": {"used_usd": 12.3}, "gemini-api": {"used_usd": 1.2}}`)
against `ANTHROPIC_API_CREDITS_USD` (200) and `GOOGLE_API_CREDITS_USD` (10). Without the file those rows say `unknown`.
