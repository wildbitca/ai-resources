# Release flow

Releases are published by the pipeline, not by hand.

## Normal path

1. A change that should ship bumps the version in four places, in the same PR:
   `scripts/ai_resources/__init__.py` (`__version__`, the source of truth),
   `.claude-plugin/plugin.json` (`version`), `Formula/ai-resources.rb` (`tag: "vX.Y.Z"` and `version "X.Y.Z"`),
   and a `## [X.Y.Z]` section in `CHANGELOG.md` with non-empty notes.
2. On the PR, the `release-check` job (dry run) runs `scripts/ci/release_check.py`. It fails when the four
   places disagree or the CHANGELOG section is missing or empty, and prints the notes it would publish.
   It publishes nothing.
3. On merge to `main`, `validate` runs on both matrix legs. The `release` job has `needs: validate`, so it only
   starts when both are green. It re-runs the check, then looks for tag `vX.Y.Z` on the remote:
   - tag exists: the job ends green without publishing (the normal case for any merge that does not bump the version);
   - no tag: it creates the tag on the merged commit and the GitHub Release, with the CHANGELOG section as notes.
4. `contents: write` is granted only to the `release` job; the workflow default stays `contents: read`.
   The job has its own concurrency group (`release`, `cancel-in-progress: false`), so two merges never race
   and a running release is never cancelled.

## Failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| `release-check` or `release` fails with a mismatch message | version files disagree | align the four places and push |
| `release` fails with "no '## [X.Y.Z]' section" or "empty" | CHANGELOG not updated | add the section and push to `main`; the new push's `release` job publishes it (do not re-run the old job: it re-runs on the broken commit) |
| `release` skipped | `validate` failed on a leg, or the run is not a push to `main` | fix `validate`; re-run |
| `release` fails at "Create tag and GitHub Release" | token or branch rules block tag creation, or the Release API failed | use the manual fallback below |
| Merge without a version bump | tag already exists | nothing to do, by design |

## Manual fallback

`.github/workflows/release.yml` is `workflow_dispatch` only (it no longer runs on tag pushes, so a tag made by
the pipeline cannot produce a second release). Run it from the Actions tab on `main`, with input `tag` = `vX.Y.Z`.
It runs only on `refs/heads/main`, runs the same consistency check, refuses a tag that differs from the version files,
refuses when a Release for the tag already exists, and refuses when the tag exists on a commit other than the run's HEAD.

## Status

The workflows cannot be run locally. `scripts/ci/release_check.py` is unit-tested (`tests/test_release_check.py`)
and the YAML is linted with `actionlint`, but the first real run of the `release` job is the merge to `main`.
