# Tasks: {feature}

> Temporal. Lives at `specs/features/{feature}/tasks.md` next to the spec it implements (or as a `## Tasks` section of a short spec). Created for one change and removed when it closes: `knowledge-audit` folds the durable parts into the spec or `specs/knowledge/decisions/` first. Not created for `inline` work. Written in English.

## Sizing
- mode: light            # inline | light | full (override: --inline / --light / --full)
- resume_test: fail      # pass | fail (why: sessions | external | separate-deliverables | compaction)
- risk_items: none       # none | [3: changes handoff front-matter keys read by hooks]
- forecast: lines~120 units=1 surfaces=[path/a.py, tests/test_a.py]
- route: inline          # inline | explorer(reason) | writers(N, reason)
- divisible: no          # yes only when the units share no state and wait on none; in doubt, no (one agent)
- reason: none           # required to delegate: explore-without-polluting-main-context | parallel-writers-on-disjoint-units | independent-verification
- verification: self     # self | verifier(item N)
- spawn_budget: 0

## Specs
Acceptance criteria, in English, each with a short `source:` reference (ticket or message link). Reuse `RULE-*` / `TS-*` ids from the spec when they exist.

- **S1**: Given ..., when ..., then ... (source: <ticket or message link>; proof: `<command>`)

## Tasks
Each task names the spec ids it covers. Hand work off by these ids and this file's path, never by paraphrase.

- [ ] **T1** (covers S1) <what to change, files> - commit: <sha when done>

## Log
- L1 <date>: original request link and any change of scope, one line each. A change rewrites only the affected `S#`.

## Sizing (actual, filled at close)
- lines: +0/-0=0 | spawns: 0 | commits: 0 | reclassified: no
