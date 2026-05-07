# Changelog

All notable changes to **ai-resources** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). **Release versions match Git tags** `vMAJOR.MINOR.PATCH`.

## [Unreleased]

## [1.1.2] — 2026-05-07 — README refresh & version command fix

### Added

- **Orchestrator Delegation Protocol** — added explicit delegation rules to
  `CLAUDE.md`: when to spawn `explore`, `generalPurpose`, or `doc-writer`
  instead of reading files directly, plus a token budget check heuristic.
- **`doc-writer` subagent (Gemini Flash)** — new agent definition for
  documentation update tasks. Handles all reading + drafting; orchestrator
  applies final diffs only.

### Changed

- **`software-architect` routed to `gemini-2.5-pro`** in `executors.yaml`
  and agent frontmatter (was `claude-sonnet-4-6`).

### Documentation

- **README** — comprehensive update for v1.1.x: highlights banner, new CLI
  flags (`--dry-run`, `--non-interactive`, `--profile`), `setup --dry-run`
  example, mode-switching teardown explanation, Claude Code OAuth section,
  expanded directory map (full `setup/` subpackage), new Profiles table,
  and `docs/multi-model.md` / `docs/litellm-service.md` in Further Reading.
- **`ai-resources version`** — now correctly reports `1.1.2` (working-tree
  `__init__.py` was inadvertently left at `1.0.0` after the v1.1.1 release).

## [1.1.1] — 2026-05-07

### Fixed

- **Formula PyPI URLs** — all 16 resource URLs updated to the hash-based path
  format now required by PyPI (legacy `/source/<letter>/…` paths return 404).
  Also corrects the `questionary` sha256 which was wrong in v1.1.0.

## [1.1.0] — 2026-05-07 — Dry-run, teardown & OAuth fixes

### Added

- **`--dry-run` flag** for `ai-resources setup` — renders all generated configs
  (`litellm.yaml`, `docker-compose.yaml`, `settings.json` patch) and starts a
  temporary gateway for live smoke tests, without writing anything to disk.
- **`InstallTracking` audit trail** (`state.py`) — records exactly what the wizard
  installed (LiteLLM method, Docker image, lifecycle unit, env keys, cockpit env
  patches). Pre-existing artifacts are never tracked, so teardown only undoes wizard
  work.
- **Multi-model → single-model teardown** — when the user switches modes, the wizard
  presents a confirmation list and cleanly removes every tracked artifact.
- **Claude passthrough models** in `litellm.yaml` — all current Claude model IDs
  (`claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`) are
  auto-added as passthrough entries when Anthropic is enabled, preventing
  `ProxyModelNotFoundError` for Claude Code internal requests.
- **`allow_requests_on_db_unavailable: true`** in gateway `general_settings` —
  lets OAuth session tokens pass through the localhost gateway without a DB lookup.
- **`credentials.update_env_tracked()` / `credentials.remove_keys()`** — new
  helpers that return which keys were net-new / removed, enabling precise teardown.
- **`_shared.env_keys_added_by_patch()` / `_shared.remove_env_keys_from_settings()`**
  — cockpit-level env teardown helpers.
- **`claude.teardown()`** — removes gateway env keys from `settings.json` on mode
  switch.
- **`claude.is_logged_in_via_oauth()`** — detects OAuth/firstParty mode via
  `claude auth status` JSON (with Keychain/credential-file fallback for older Claude
  Code versions). Used by wizard completion banner and `ai-resources doctor`.
- **`credentials.get_claude_code_keychain_key()` / `credentials.write_claude_code_keychain_key()`**
  — macOS Keychain helpers for reading/restoring the Claude Code API key.
- **`claude.fix_oauth_for_gateway()`** — automated logout → restore flow on macOS:
  saves key from Keychain, runs `claude logout`, writes key back so Claude Code
  starts in API key mode without prompting.
- **New profile `quality-first.yaml`** — replaces `unified-default.yaml`.
  Identical routing strategy; renamed for clarity.
- **`doctor` OAuth awareness** — in multi-model mode, shows an info notice when
  Claude Code is in OAuth mode with actionable guidance.

### Fixed

- **`vertex-enterprise.yaml` model IDs** — removed incorrect `@date` suffix from
  `claude-opus-4-7` and `claude-sonnet-4-6` (bare alias is correct per Vertex AI
  docs); `claude-haiku-4-5@20251001` retains the required suffix.
- **Provider model lists** (`providers.py`) — added `gpt-5-nano`, `gpt-4o-mini`,
  `gemini-2.5-flash-lite`; Vertex list now mirrors these.
- **Default profile in single-model mode** changed from `all-claude` to
  `cost-optimized`.
- **State deserialization** for nested dataclasses (`SetupState` → `InstallTracking`
  and other nested fields) now handled correctly via `_from_dict` registry.
- **Python 3.14+ compatibility** in dependency detection.
- **pip-venv as default LiteLLM install mode** — `pipx` no longer required for
  first-time installs; a dedicated virtualenv at
  `~/.config/ai-resources/venv/` is used instead.
- **Better LiteLLM install error reporting** — verbose log dump on failure.
- **Wizard auto-installs pipx** when selected as install method and pipx is absent
  (cross-platform: Homebrew on macOS, `pip install --user pipx` elsewhere).

### Changed

- `ANTHROPIC_API_KEY` removed from the Claude Code `settings.json` env patch in
  multi-model mode — Claude Code reads its key from the macOS Keychain and the
  gateway now uses `allow_requests_on_db_unavailable` instead.

### Skills

- All community and curated skills: bumped upstream `git_revision` to latest;
  removed redundant `(triggers: …)` inline comment from SKILL.md descriptions.

## [1.0.0] — 2026-05-07 — Multi-model orchestration

Major refactor introducing per-role LLM routing via local LiteLLM gateway.
**Breaking changes** in CLI surface; one-time migration via `ai-resources setup`.

### Added

- **Multi-model mode** — each subagent role runs on a different LLM (Claude,
  Gemini, GPT, Vertex, Ollama) routed through LiteLLM. See
  `docs/multi-model.md` for the full guide.
- **Interactive setup wizard** (`ai-resources setup`) — 9-step `rich` +
  `questionary` UI: cockpit detection, LiteLLM install, provider creds,
  profile selection, per-role customization, lifecycle, smoke tests.
  Re-runs use prior answers as defaults.
- **LiteLLM gateway** — pipx-managed process (default) or remote endpoint.
  No Docker required. Service managed by launchd (macOS) / systemd-user (Linux).
  Lifecycle via `ai-resources daemon {start,stop,status,logs,update}`.
  Auto-start at every login. Wrapper script sources `.env` (chmod 600) and
  execs `litellm` directly — secrets never touch the service file.
- **Profiles** — `unified-default`, `all-claude`, `all-gemini`,
  `cost-optimized`, `vertex-enterprise` at `<kit>/profiles/*.yaml`.
- **`ai-resources doctor`** — full health check across config, credentials,
  gateway, cockpits, and live model round-trips.
- **`ai-resources executors {show,edit,test}`** — inspect or edit role→model
  mapping; test a single role's round-trip.
- **`ai-resources audit`** — cost report from gateway logs (per role / model).
- **Cockpit configurators** — modular per-cockpit setup for Claude Code,
  Cursor, Gemini CLI, Codex, Aider, Copilot, Windsurf, Continue.dev,
  OpenCode. Each writes its canonical config + multi-model protocol section.
- **Engram MCP in Gemini CLI** — same schema as Claude Code; both clients
  hit the same memory backend regardless of cockpit.
- **`rules/017-multimodel-routing.mdc`** — kit-internal rule explaining
  the multi-model architecture.
- **Setup state** — `~/.config/ai-resources/setup-state.yaml` persists
  wizard answers; `executors.yaml`, `litellm.yaml`, `docker-compose.yaml`,
  `.env` (chmod 600) live alongside.

### Changed

- **Package layout** — `scripts/kit.py` is now a thin shim. All logic moved
  to `scripts/ai_resources/` package: `cli.py`, `generate.py`, `daemon.py`,
  `doctor.py`, `executors_cmd.py`, `audit.py`, plus `setup/` subpackage with
  `wizard.py`, `state.py`, `detection.py`, `ui.py`, `credentials.py`,
  `litellm.py`, `providers.py`, `profiles.py`, `smoke.py`, `install.py`,
  and `cockpits/{claude,gemini,cursor,codex,aider,copilot,windsurf,continue_dev,opencode}.py`.
- **Subagent `model:` field** — now passes through verbatim to the gateway.
  Legacy `strong`/`fast`/`inherit` mapping preserved for single-model mode.
- **Homebrew formula** — uses `Language::Python::Virtualenv` to install
  `rich`, `questionary`, `prompt_toolkit`, `pyyaml`, `httpx` (and transitive
  deps) into a venv at `libexec/venv/`. The `ai-resources` wrapper uses
  this venv.
- **`rules/050-subagent-delegation.mdc`** — new section linking to
  multi-model routing rule.

### Removed

- `_MODEL_MAP` constraint to opus/haiku/inherit — replaced by passthrough.
- `_STUB_TARGETS` monolithic dict and `_setup_*` per-target functions —
  replaced by `cockpits/*.py` module registry.
- `cmd_setup` monolith from `kit.py` — replaced by `wizard.py`.
- Argparse-only CLI surface — `setup` is now interactive by default.

### Migration

- v0.7.x users: `ai-resources setup` detects existing single-mode config and
  offers to upgrade. State is migrated to new `setup-state.yaml`.
- The legacy `python3 scripts/kit.py setup --target claude` style still
  works (delegated to the new wizard).

## [0.7.1] — 2026-04-10

### Removed

- **Workflow enforcement hook**: removed the `UserPromptSubmit` prompt hook that blocked trivial/conversational messages. The hook ran an LLM without conversation context on every message, causing false positives that blocked simple questions with "Operation stopped by hook". Workflow routing is already handled by the Workflow Discovery Protocol in CLAUDE.md — which has full conversation context and is more accurate.
- **`_build_hooks_config()`** and **`_merge_settings_with_hooks()`**: no longer needed without the hook.

### Added

- **`_cleanup_stale_hooks()`**: `kit.py setup` now removes stale `UserPromptSubmit` hooks from `settings.json` left by v0.7.0, so users don't need to manually clean up.

## [0.7.0] — 2026-04-10 [YANKED]

### Added

- **Workflow enforcement hook** _(yanked — blocked conversational messages)_: `kit.py setup --target claude` installed a `UserPromptSubmit` prompt hook. Removed in v0.7.1 due to false positives.
- **`_build_hooks_config()`**: generates the hooks structure dynamically from scanned workflow triggers.
- **`_merge_settings_with_hooks()`**: replaces `_merge_json_file` for settings.json to handle both env vars and hooks (array-based deep merge).

### Changed

- **`_setup_claude()`**: now calls `_merge_settings_with_hooks()` instead of `_merge_json_file()` for settings.json, merging env vars and hooks in a single operation.

## [0.5.1] — 2026-04-01

### Added

- **Agent Team blueprints**: 7 pre-defined team compositions (Feature Development, Bug Fix, Security Audit, Production Triage, Infrastructure & Platform, Dependency Maintenance, Code Review) generated into `~/.claude/CLAUDE.md`. Each blueprint includes: trigger conditions, teammate list with roles, workflow sequence, parallel groups, and domain persona hints.
- **Teams vs Subagents guidance**: CLAUDE.md now includes a decision table and step-by-step instructions for when and how to create Agent Teams via `TeamCreate` + `SendMessage`.
- **Specialist agents in `_ROLE_TOOLS`**: `crashlytics-fixer`, `sentry-fixer`, `package-upgrade`, `terraform-maintainer`, and `crossplane-upjet-maintainer` now explicitly listed with full tool access (`None`).

### Changed

- **`_setup_claude()` refactored**: replaced inline agent-table + teams stub (20 lines) with `_build_teams_section()` function that generates the full agent catalog and team blueprints documentation.

## [0.5.0] — 2026-04-01

### Added

- **Claude Code subagent generation**: `kit.py setup --target claude` now auto-generates Claude Code subagent definitions (`~/.claude/agents/*.md`) from `agents/roles/*.md`. Each role gets proper tool restrictions and model mapping (e.g. `strong` → `opus`). Cursor-specific references are rewritten automatically.
- **Claude Code settings.json management**: setup merges `AGENT_SKILLS_ROOT` and `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` into `~/.claude/settings.json` idempotently — no manual env vars or config needed.
- **Agent Teams support**: enabled automatically by setup. Workflow steps with matching `execution_hints.parallel_group` can run as concurrent Agent Team teammates.
- **`execution_hints` in workflow YAML**: new optional field for runtime-aware parallelism. Steps sharing a `parallel_group` name can run concurrently on capable runtimes (Claude Code Agent Teams, Cursor parallel Composers). Runtimes without support ignore hints and execute sequentially.
- **WORKFLOW_CONTRACT.md**: documented `execution_hints` schema with runtime interpretation table.

### Changed

- **Refactored setup dispatch**: replaced 9 per-target `_write_*_stub` functions + 4 Claude-specific helpers (13 functions) with a data-driven `_STUB_TARGETS` table, one `_write_stub()`, and one `_setup_claude()`. Net result: 43 → 38 functions with more functionality.
- **New reusable primitives**: `_merge_json_file()` (idempotent JSON merge), `_ensure_symlink()` (idempotent symlinks), `_install_claude_plugin()` (generic plugin installer). All usable by future targets.
- **Feature workflow** (`_feature-template`): `test`, `review`, `security` steps now have `parallel_group: post-implement`.
- **Bugfix workflow** (`_bugfix-template`): `test`, `security` steps now have `parallel_group: post-fix`.

## [0.4.0] — 2026-03-24

### Added

- **Orchestration guide** (`docs/orchestration.md`): detailed documentation with 10 Mermaid diagrams covering all workflow pipelines, skill discovery flow, subagent delegation, handoff protocol, conditional branching, and domain detection.
- **Expanded README**: directory map, skill categories table, workflow overview, agent roles/personas reference, orchestration diagram, key concepts (skill/workflow discovery, zero-trust engineering).
- **Skill triggers for all 116 skills**: every skill now has meaningful `triggers` keywords (file globs, technology names, action phrases) for accurate auto-discovery. Previously 61/116 had empty triggers.
- **Inline frontmatter parsing** in `kit.py`: the index generator now supports `triggers: "value"` (inline string) and multi-line YAML `description: >` with embedded `(triggers: ...)`, in addition to YAML list format.

### Changed

- **Full English translation**: all documentation, CLI messages, script output, CHANGELOG, README, SKILL.md files, and Python user-facing strings translated from Spanish to English. Includes version-commit.py prompts (`s/n` → `y/n`), reason messages, labels, and comments.
- **normalize-changelog.py**: added English summary keys alongside legacy Spanish keys for backward compatibility with existing changelogs.

## [0.3.1] — 2026-03-24

### Added

- **Workflow Discovery Protocol:** `_discovery_recipe()` now generates a "Workflow Discovery Protocol" section **before** Skill Discovery in every agent stub. The section includes a dynamically-built trigger table (scanned from `workflows/*.workflow.yaml` at setup time) and a MANDATORY rule that forbids substituting built-in tools (e.g. `EnterPlanMode`, ad-hoc Plan agents) for workflow-defined phases.
- **`_scan_workflow_triggers()` helper:** reads all `.workflow.yaml` files and extracts `name` + `trigger` fields for the generated table.
- **`_workflow_recipe()` helper:** builds the Workflow Discovery Protocol markdown from scanned triggers — single source, shared across all 9 agent stubs.
- **Workflow contract** added to Key paths table in generated stubs.

### Changed

- **All 9 agent stubs updated:** Claude Code, Cursor, Gemini CLI, OpenCode, Codex, GitHub Copilot, Windsurf, Continue.dev, and Aider now receive workflow discovery instructions alongside skill discovery — workflows are evaluated first.

## [0.3.0] — 2026-03-24

### Added

- **Universal Skill Discovery Protocol:** every agent stub now includes a 5-step discovery recipe that tells the agent how to read `skills-index.json`, match skills by description/triggers, and load the relevant `SKILL.md` files on demand. This replaces the previous minimal pointer stubs.
- **New setup targets:** `windsurf` (`~/.codeium/windsurf/memories/global_rules.md`), `continue` (`~/.continue/AGENT_KIT.md`), `aider` (`~/.aider/CONVENTIONS.md`). Total: 9 agent targets + MCP + symlinks.
- **Skill symlinks for native discovery:** `kit.py setup` creates symlinks from `~/.claude/skills/ai-resources` and `~/.agents/skills/ai-resources` (Codex) to the centralized `skills/` directory, enabling native SKILL.md discovery in agents that support it.
- **`_discovery_recipe()` helper:** single-source function that generates the discovery instructions, shared across all agent stubs for consistency.

### Changed

- **All existing stubs rewritten:** Claude Code, Cursor, Gemini CLI, OpenCode, Codex, and GitHub Copilot stubs now include the full discovery protocol and refresh hints (previously only had minimal path references).
- **`cmd_setup` refactored:** dispatch table replaces if/elif chain — easier to extend with new agent targets.
- **Stub function signatures unified:** all `_write_*_stub` functions now receive `(ak_s, hint, *, dry_run)` for consistency.
- **CLI `--target` choices expanded:** added `windsurf`, `continue`, `aider` to the argparse choices.

## [0.2.1] — 2026-03-23

### Changed

- **Skills, agents, roles, workflows:** removed all references to internal projects, companies, and repository names; kit is now fully agnostic and reusable by any team or organization.
- **`flutter-icons` skill:** brand color placeholder (`#006a64`) in config examples now carries explicit `← Replace with your brand color` labels on every occurrence to prevent agents from applying it literally.
- **Terraform scripts:** `upgrade-providers.py` regex now matches any Terraform Cloud org (not hardcoded); `upgrade-all-with-deps.py` renamed `WILDBIT_MODULE_RE` → `TF_PRIVATE_MODULE_RE`.

### Fixed

- **`terraform-version-commit` script:** removed non-generic auto-detect fallback for dependent projects; `--dependent-projects` must now be passed explicitly (safe default: no dependents processed if omitted).
- **`maintain.sh`:** dependent-projects argument is now only forwarded when `DEPENDENT_PROJECTS` env var is set (no fictitious default path).

## [0.2.0] — 2026-03-23

### Added

- **Engram as Homebrew dependency:** `Formula/ai-resources.rb` declares `depends_on "gentleman-programming/tap/engram"` — `brew install ai-resources` installs the `engram` binary automatically.
- **Automatic engram setup for Claude Code:** `kit.py setup` (targets `claude` and `all`) runs `claude plugin marketplace add Gentleman-Programming/engram && claude plugin install engram`, registering the MCP server, hooks, and the Memory Protocol skill without manual steps.
- **MCP engram in Cursor:** `resources.json` → `mcp.cursor.mcpServers` includes the `engram` entry (`engram mcp` stdio); applied when running `kit.py setup`.

### Changed

- `Formula/ai-resources.rb`: tag and version updated to `v0.2.0`; removed `revision 1` (it was a build patch, not applicable to the new version).

## [0.1.0] — 2026-03-23

First **usable** release with Homebrew. The previous **`v0.1.0`** tag did not install correctly (nonexistent tap / misplaced formula / `brew tap` without URL pointing to another repo); the current tag includes **`Formula/ai-resources.rb`** at the root and the correct tap command.

### Added

- Unified CLI `scripts/kit.py`: **`generate`** (import from `resources.json` + `skills-index.json`), **`setup`** (MCP, IDE stubs, workflow validation).
- Manifest `resources.json`; optional state `~/.config/ai-resources/state.json` after `setup`.
- **Homebrew:** formula **`Formula/ai-resources.rb`** at the root; **`brew tap wildbitca/ai-resources https://github.com/wildbitca/ai-resources.git`** (URL is required: without it Homebrew looks for `wildbitca/homebrew-ai-resources`) + **`brew install ai-resources`**. Installation via **git** at the tag (no `sha256` tarball). Private repo: `HOMEBREW_GITHUB_API_TOKEN`.

### Changed

- **README** focused on installation and usage with minimal commands.
- **Documentation:** history in this file; releases without helper scripts in repo (see checklist below). Removed `RELEASING.md`, `packaging/homebrew/README.md`, `tag-release.sh`, `bump-formula-sha.sh`.
- **Homebrew:** same repository for code and tap (separate `homebrew-ai-resources` no longer needed); removed `packaging/homebrew/`.

### Fixed

- **`ai-resources` command:** the wrapper no longer uses a fixed path to `opt/python@3.12/bin/python3` (which might not exist); it adds the `bin` of `python@3.12` to `PATH` and uses **`python3`** or **`python3.12`**. Formula **`revision 1`**.

---

When you publish **`vMAJOR.MINOR.PATCH`**, add a new section above `[Unreleased]` with that version and date, then move the relevant items from `[Unreleased]` into it.

## Release manager checklist

1. Choose SemVer and move entries in **`CHANGELOG.md`**.
2. Update **`Formula/ai-resources.rb`:** `tag:` and `version` matching the new tag.
3. Bump **`scripts/ai_resources/__init__.py`** `__version__` to match.
4. Commit to `main` and **push**.
5. **Tag and push:** `git tag -a vX.Y.Z -m "Release X.Y.Z"` · `git push origin vX.Y.Z`
6. **GitHub Release is created automatically** by `.github/workflows/release.yml` when the tag is pushed. Release notes are extracted from the matching `## [X.Y.Z]` block in this file.

Users upgrade with: `brew update && brew upgrade ai-resources` (private repo: `HOMEBREW_GITHUB_API_TOKEN`). The **README** must keep **`brew tap wildbitca/ai-resources https://github.com/wildbitca/ai-resources.git`** (do not omit the URL).
