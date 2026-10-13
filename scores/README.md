# scores/

The kit-shipped model scores database used by `ai-resources setup` to propose a team (ADR-0006).

- `model-scores.yaml` is read-only. Edit it only to refresh evidence, with a source and a date per row.
- User overrides live in `$XDG_CONFIG_HOME/ai-resources/model-scores.yaml` (default `~/.config/ai-resources/`).
  An override row must carry `reason` and `as_of`; it shadows a shipped row by key and is flagged in every report.
- `ai-resources models scores show|check|diff|refresh` reads and reports; `check` exits 1 when anything is listed.

## Schema (version 1)

| Block | What it holds |
|---|---|
| `schema_version`, `dataset_version` | Independent of the kit version. |
| `policy` | **POLICY, not evidence**: role importance, tolerance classes (critical 0.3, standard 0.5, low 1.0), role class, load weight, `diversity_tolerance`, `loss_cap`, reference tokens, static headroom levels. |
| `routes` | The route vocabulary: `anthropic-direct`, `vertex-global`, `google-ai-studio`, `openrouter`, `litellm`, `openclaw-codex-engine`, each with its surface and provider. |
| `surfaces` | Per cockpit or engine `usage`: `dispatch-ok`, `direct-only` or `instructions-only`, with a reason and a citation. |
| `pools` | Billing-class pools: `billing_class`, `routes`, `windows`, `headroom_source`, `model_weights` (qualitative for the Claude subscription), `headless_draws_plan`, `credits_cache_ttl_minutes`, `hard_stop_errors`, `usage`, `source_ref`. |
| `models` | Canonical `<vendor>/<id>` (the spelling profiles use), `status`, `route_ids` (the id each route uses), dated `prices`, role-agnostic `benchmarks`, `hazards`. |
| `aliases` | Name suggestions, labelled UNKNOWN when unconfirmed. |
| `rows` | `(role, model, route, effort)` -> `score` 0-10 or null, `label`, `as_of`, `source_ref`, `basis` (required for INFERRED), `expires_after_days` (default 90), `conditions`, `notes`. A row may list several routes; the loader expands it. |

Rules the validator (`scoring/db.validate`, run by `tests/test_scores_schema.py` and `models scores check`) enforces:

- no `verified` key anywhere: verification comes only from `setup/compat.py`;
- no stored `routable`: it is computed from `setup/providers.KNOWN_MODELS`;
- every row has a label, an ISO `as_of` and a `source_ref`; INFERRED rows name their `basis`; MEASURED-HERE rows carry `run_id`, `n`, `harness_version`, `corpus_hash` and `low_power`;
- price windows of a model and route never overlap, and a price is never invented (unknown stays null);
- a proxy is `MEASURED-3P` and `role_agnostic: true`; a role row derived from one is `INFERRED`.

## Evidence labels

`MEASURED-HERE` (this kit's harness, Phase B) > `MEASURED-3P` (a named board, dated) > `VENDOR` > `INFERRED` > `UNKNOWN`. A composed score carries the weakest contributing label.

## Seeding rules

- Every non-null score cites `file:line` under `.agent-output/research-models/`.
- Conflicts cite both sources and take the lower value, except where the plan names the dated source.
- The operator's earlier illustrative numbers, TB 2.x figures, the 2026-09-16 kit runs and Opus 5 MacroscopeBench are never seed data.
- Review, verification and planning have no independent benchmark: those rows are `INFERRED` placeholders from a role-agnostic proxy and say so in `basis`.
