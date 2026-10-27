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

## Balancing

`routing.choose_pool(class, usage)`: the first pool of the class under 85% used serves it; unknown counts as available; if all
are hot, the least used. `ai-resources doctor` prints each pool's usage and where every class would run now.

## Feeding the credit pools

Doctor reads `~/.cache/ai-resources/api-spend.json` (`{"anthropic-api": {"used_usd": 12.3}, "gemini-api": {"used_usd": 1.2}}`)
against `ANTHROPIC_API_CREDITS_USD` (200) and `GOOGLE_API_CREDITS_USD` (10). Without the file those rows say `unknown`.
