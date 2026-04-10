# Changelog

All notable changes to **ai-resources** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). **Release versions match Git tags** `vMAJOR.MINOR.PATCH`.

## [Unreleased]

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
3. Commit to `main` and **push**.
4. **Tag and push:** `git tag -a vX.Y.Z -m "Release X.Y.Z"` · `git push origin vX.Y.Z`
5. Optional: GitHub Release. Users: `brew update && brew upgrade ai-resources` (private: `HOMEBREW_GITHUB_API_TOKEN`). The **README** must keep **`brew tap wildbitca/ai-resources https://github.com/wildbitca/ai-resources.git`** (do not omit the URL).
