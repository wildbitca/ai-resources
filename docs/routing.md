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
| `~/.config/ai-resources/lanes.yaml` (`routing.matrix`, digest, Google ack) | `setup/lanes_render.py` |
| `agents/roles/*.md` marked `model:` block (Claude Code hosts the role on its first Max cell) | `ai-resources generate` (`generate._sync_role_models`) |
| OpenClaw `kit-role-*` agents: `model.primary` + `model.fallbacks`, Max cells pinned to the `claude-cli` runtime, agy cells as `agy-cli-lane/<slug>` | `setup/cockpits/openclaw_roles.py` |
| the setup role table | `routing.table_rows` |

Never edit a generated block by hand.

## Balancing

`routing.choose_pool(class, usage)`: the first pool of the class under 85% used serves it; unknown counts as available; if all
are hot, the least used. `ai-resources doctor` prints each pool's usage and where every class would run now.

## Feeding the credit pools

Doctor reads `~/.cache/ai-resources/api-spend.json` (`{"anthropic-api": {"used_usd": 12.3}, "gemini-api": {"used_usd": 1.2}}`)
against `ANTHROPIC_API_CREDITS_USD` (200) and `GOOGLE_API_CREDITS_USD` (10). Without the file those rows say `unknown`.
