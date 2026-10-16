# Release 2.2.0 checklist (prepared, NOT published)

State at preparation: the version is bumped to 2.2.0 in `scripts/ai_resources/__init__.py`,
`.claude-plugin/plugin.json` and `Formula/ai-resources.rb` (`version` and `tag: "v2.2.0"`). `CHANGELOG.md` has a
`[2.2.0]` section and a fresh empty `[Unreleased]`. No tag exists, nothing was pushed, no release was created and
the Homebrew formula was not installed anywhere. The operator runs every command below.

Note on the formula: `Formula/ai-resources.rb` installs the kit from a git tag (`using: :git, tag:`), so it has
**no kit tarball sha256 to update**. The `sha256` lines in the file belong to the pinned PyPI resources (rich,
questionary, prompt_toolkit, ...) and do not change for this release. If the formula ever moves to a tarball
`url`, take its sha256 from the release asset: `curl -sL <tarball-url> | shasum -a 256`.

## 0. Preconditions

```bash
cd ~/Development/wildbit/ai-resources
git status --short                      # only handoff.md may be untracked; it is never committed
git log --oneline -15                   # review the commits since v2.1.1
git diff v2.1.1..HEAD --stat | tail -3
```

Run the CI gate in the project venv (all must pass):

```bash
python3 scripts/validate_kit.py
python3 -m compileall -q scripts
for f in scripts/openclaw/*.sh; do bash -n "$f" || echo "SYNTAX: $f"; done
~/.venvs/ai-resources-test/bin/python -m pytest -q -p no:cacheprovider tests
```

## 1. Tag, push, release

```bash
git tag -a v2.2.0 -m "ai-resources 2.2.0"
git push origin main
git push origin v2.2.0
gh release create v2.2.0 --title "ai-resources 2.2.0" --notes-file <(sed -n '/^## \[2.2.0\]/,/^## \[2.1.1\]/p' CHANGELOG.md | sed '$d')
```

Push the branch and the tag together: `main` already names `tag: "v2.2.0"` in the formula, so a pushed `main` without
the tag breaks `brew install` from `main` until the tag lands.

## 2. Homebrew

```bash
brew update
brew upgrade ai-resources              # or: brew reinstall ai-resources
ai-resources version                   # expect: ai-resources 2.2.0
ai-resources setup                     # refreshes skills, subagents and hooks (operator-run; see ADR-0003: never restarts the gateway)
```

## 3. Post-install smoke

```bash
ai-resources doctor                    # sub-check 4e (MCP children) runs only when lanes are enabled
ai-resources setup --dry-run           # masked diff, "DRY RUN: nothing was written"
ai-resources setup --help              # lists the steps, keys and lanes flags
ai-resources lanes verify --help       # lanes verify needs a run; with lanes off, nothing to check
ai-resources setup --lanes-dry-run     # proposed lane table, writes nothing
```

`ai-resources lanes verify` itself needs a prepared step (`--step`, full `--subject` sha). With lanes disabled
the command has nothing to verify; that is not a failure.

## 4. Proven vs not proven

Proven (deterministic offline tests in this repository):

- the masking rules, including `password is X` prose and a value after an empty `KEY=`;
- `setup --help` content; the lanes resolver, held backends (agy, codex), `lanes prepare` refusals (E1: entry
  without a model; E2: `alsoAllow` and `group:*`; E3: admission only against the file's own agents);
- the headroom reader's two paths (measured, static fallback) and its determinism;
- the doctor MCP-children count against a synthetic cgroup tree; evidence and effort metadata.

NOT proven (needs the live host or the isolated merge gate; implemented behind safe defaults):

- that `tools.deny: ["bundle-mcp"]` stops supabase/engram accumulation on live lane children;
- that OpenClaw accepts a `codex-cli` runtime catalog entry (the lane is held and never written);
- the accuracy of the status-line sidecar headroom over a day of real use;
- the claude-cli lane end to end on the live gateway (ADR-0007 merge gate); commit `14bdcfd` was verified by an
  independent Verifier by code reading and tests (E1-E3), not on a live host;
- the Antigravity lane gates G0-G5 (merge gate record, V5 slugs, V6 sandbox, V7 tool/MCP surface, V9 workspace,
  V12 agy home) are NOT proven and G6/G7 are partial, so the agy lane stays closed even though ADR-0007 section 11 is
  accepted;
- `ai-resources setup` on a real host after `brew upgrade` (never run by the preparation).

## 5. Rollback

```bash
brew uninstall ai-resources
git -C "$(brew --repo wildbitca/ai-resources)" checkout v2.1.1 -- Formula/ai-resources.rb
brew install --formula "$(brew --repo wildbitca/ai-resources)/Formula/ai-resources.rb"   # back on 2.1.1
ai-resources setup --no-lanes          # drops lanes.yaml and the kit-lane-* agents the kit created
git push origin :refs/tags/v2.2.0      # only if the tag must be withdrawn
gh release delete v2.2.0 --yes         # only if the release must be withdrawn
```

State files under `~/.config/ai-resources/` are additive and tracked; `setup --no-lanes` removes what lanes wrote.
Landed lane commits stay as normal commits (ADR-0007 rollback). Revert code with `git revert <sha>` per commit.
