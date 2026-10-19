"""Profile names: the preset ids, the old names that still load, and the one resolver that knows the layout.

ADR-0010 PD-6. A starting team has the same id on every row: `balanced`, `performance`, `low-cost`. Where the
file lives depends on how the host reaches its models:

- no gateway (the CLI team and "each on its own"): `profiles/<id>.yaml`
- a gateway (OpenRouter): `profiles/gateway/<id>.yaml`

`resolve` is the only code that knows that layout. A bare preset id therefore needs a mode, and loading one without
it raises `ProfileModeRequired` rather than guessing the family (a gateway would receive bare Claude aliases, which
its catalogue does not know).
"""
from __future__ import annotations

from pathlib import Path

PRESET_IDS: tuple[str, ...] = ("balanced", "performance", "low-cost")

GATEWAY_DIR = "gateway"

# Old name -> new name, accepted forever. An alias is admitted only when tests/test_profile_names.py proves that the
# old and the new name resolve to the same runtime team with the lane gates closed AND that the new team adds no
# provider or data path the old one lacked.
ALIASES: dict[str, str] = {
    "subscription-performance": "performance",
    "subscription-economy": "low-cost",
    "performance-claude-55": "gateway/performance",
    "economy-claude-55": "gateway/low-cost",
}

# Old name -> preset id, used for the preset question's default only. These profiles differ in content from the new
# preset, so their own file keeps loading and a non-interactive rerun never changes a saved team silently.
LEGACY_PRESET: dict[str, str] = {
    # Its team diverged from `balanced` (Gemini over agy on four roles, ADR-0010 S6), so it is not an alias: the
    # pre-rename file is kept and a host saved under this name keeps its Claude-only team.
    "subscription-claude-55": "balanced",
    "claude-native": "balanced",
    "balanced-claude-55": "balanced",
    "openrouter-balanced": "balanced",
}

# The internal preset key that was renamed: `low-pricing` is still accepted wherever a key is read.
PRESET_KEY_ALIASES: dict[str, str] = {"low-pricing": "low-cost"}

class ProfileModeRequired(ValueError):
    """A bare preset id was loaded without saying whether the host has a gateway."""


def canonical(name: str) -> str:
    """The new name for an old one; any other name is returned as it is."""
    return ALIASES.get(name, name)


def preset_key(key: str) -> str:
    """A preset key as the wizard stores it (`low-pricing` reads as `low-cost`)."""
    return PRESET_KEY_ALIASES.get(key, key)


def legacy_preset(name: str) -> str:
    """The preset a saved profile name stands for, or "" when it stands for none."""
    target = canonical(name)
    if target in PRESET_IDS:
        return target
    if target.startswith(GATEWAY_DIR + "/") and target[len(GATEWAY_DIR) + 1:] in PRESET_IDS:
        return target[len(GATEWAY_DIR) + 1:]
    return LEGACY_PRESET.get(name, "")


def resolve(name: str, mode: str | None, root: Path) -> Path:
    """The file of profile `name` under `root` (the kit's `profiles/` directory).

    `mode` is the profile mode of the host: `"multi-model"` (a gateway) or `"single-model"`. It is only read for a
    bare preset id."""
    target = canonical(name)
    if name in ALIASES:
        mode = "single-model"          # an old name already says which family it was: it never was a gateway id
    if target in PRESET_IDS:
        if mode is None:
            raise ProfileModeRequired(
                f"profile {name!r} is a starting-team id: say whether the host has a gateway "
                f"(mode='multi-model' or 'single-model') to load it")
        if mode == "multi-model":
            target = f"{GATEWAY_DIR}/{target}"
    return root / f"{target}.yaml"
