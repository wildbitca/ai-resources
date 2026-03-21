# Semver Bump Rules for Terraform

**Principle: Minor versions must be backward compatible (BC). If anything breaks or changes the contract, bump major.**

## MAJOR (breaking or non‑BC)

- Removed `resource`, `data`, `module`, `variable`, `output`
- Renamed resource, variable, or output
- Changed variable (type, default removed, made required)
- Changed output value or description in a way that breaks consumers
- Provider major version bump (e.g. aws ~> 5.0 → ~> 6.0)
- Change that requires `terraform state rm` or manual migration
- Any change that forces callers to change their module inputs, state, or config
- **When in doubt whether a change is BC:** use major

## MINOR (additive, 100% BC only)

- New `resource`, `data`, `module` (no removals or renames)
- New **optional** variable (with default) so existing callers are unchanged
- New output (no change to existing outputs)
- New attribute on existing resource (optional or with default)
- Deprecation only (mark as deprecated, do not remove)
- If a new variable has no default, that is **breaking** → use major instead

## PATCH (fix, no contract change)

- Typo, comment, description change
- Provider patch/minor within same major
- Bug fix that does not change schema or interface
- Documentation-only changes
