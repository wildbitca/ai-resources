"""The user's model selection: WHAT they want (providers, slots, primary), not HOW it moves.

`setup-state.yaml` owns this record; the wizard writes it and `models update` only reads it. The
overlay (`model-pins.json`) owns how each slot moves. This module is pure data: no I/O, no imports
from `setup`, so `models`, `model_pins` and the wizard can all use it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SHAPES = ("single", "single-provider", "multi-provider")
TRACKS = ("family", "fixed")
SMOKE_PATHS = ("claude-cli", "litellm", "openrouter", "direct")


@dataclass
class SlotSel:
    ref: str = ""                 # "<provider>/<model id>", for example google/gemini-3.8-flash
    track: str = "family"         # family: follow the newest stable id; fixed: stay on `ref`


@dataclass
class ProviderSel:
    enabled: bool = True
    detected_via: list[str] = field(default_factory=list)
    vendor_listing: bool = False  # opt in to the vendor /models endpoint (OpenAI, DeepSeek, Moonshot)


@dataclass
class Selection:
    shape: str = "single"
    providers: dict[str, ProviderSel] = field(default_factory=dict)
    slots: dict[str, SlotSel] = field(default_factory=dict)
    primary: str = ""
    smoke_path: str = "claude-cli"
    openclaw_engine: str = ""
    allow_unverified: bool = False

    def enabled_providers(self) -> list[str]:
        return [p for p, s in self.providers.items() if s.enabled]

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "providers": {p: {"enabled": s.enabled, "detected_via": list(s.detected_via),
                              "vendor_listing": s.vendor_listing} for p, s in self.providers.items()},
            "slots": {k: {"ref": v.ref, "track": v.track} for k, v in self.slots.items()},
            "primary": self.primary,
            "smoke_path": self.smoke_path,
            "openclaw_engine": self.openclaw_engine,
            "allow_unverified": self.allow_unverified,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Selection | None":
        if not isinstance(data, dict) or not data.get("slots"):
            return None
        providers = {
            p: ProviderSel(enabled=bool(v.get("enabled", True)), detected_via=list(v.get("detected_via") or []),
                           vendor_listing=bool(v.get("vendor_listing", False)))
            for p, v in (data.get("providers") or {}).items() if isinstance(v, dict)
        }
        slots = {
            k: SlotSel(ref=str(v.get("ref", "")), track=v.get("track", "family") if v.get("track") in TRACKS else "family")
            for k, v in data["slots"].items() if isinstance(v, dict)
        }
        return cls(shape=data.get("shape") if data.get("shape") in SHAPES else "single", providers=providers,
                   slots=slots, primary=str(data.get("primary", "")),
                   smoke_path=data.get("smoke_path") if data.get("smoke_path") in SMOKE_PATHS else "claude-cli",
                   openclaw_engine=str(data.get("openclaw_engine", "")),
                   allow_unverified=bool(data.get("allow_unverified", False)))


def implicit_selection() -> Selection:
    """What a host with no recorded selection runs today: the four Claude slots on the Claude CLI."""
    from . import model_pins
    return Selection(
        shape="single",
        providers={"anthropic": ProviderSel(enabled=True, detected_via=["claude-cli"])},
        slots={model_pins.slot_key(c): SlotSel(ref=model_pins.openclaw_ref(model_pins.DEFAULTS[c]), track="family")
               for c in model_pins.CLASSES},
        primary=model_pins.slot_key("sonnet"), smoke_path="claude-cli")


def derive_shape(providers: list[str], slot_count: int) -> str:
    """single: one slot. single-provider: one provider, several slots. multi-provider otherwise."""
    if len(providers) > 1:
        return "multi-provider"
    return "single" if slot_count <= 1 else "single-provider"
