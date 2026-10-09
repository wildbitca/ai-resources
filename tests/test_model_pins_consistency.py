"""Drift guard: the hand-written Claude ids must follow model_pins.DEFAULTS.

Profiles, litellm, providers and aider keep their own spelling of a model id (the profile file format
does not change). This test fails when one of them names a Claude id that is not a rendering of the
kit default for its class, unless the id is listed in LEGACY with a reason.
"""
from __future__ import annotations

import pathlib
import re

from ai_resources import model_pins as mp

REPO = pathlib.Path(__file__).resolve().parents[1]
SCANNED = sorted(str(p.relative_to(REPO)) for p in (REPO / "profiles").glob("*.yaml")) + [
    "profiles/openclaw-host.json5",
    "scripts/ai_resources/setup/litellm.py",
    "scripts/ai_resources/setup/providers.py",
    "scripts/ai_resources/setup/cockpits/aider.py",
]
_ID = re.compile(r"claude-(?:opus|sonnet|haiku|fable)-[0-9][0-9a-z.@]*(?:-[0-9a-z.@]+)*")

# (file, id) -> reason. Keep each entry short and justified; remove it when the id goes away.
LEGACY = {
    ("scripts/ai_resources/setup/providers.py", "claude-haiku-4-5@20251001"):
        "Vertex AI spells the dated haiku release with @date",
}


def allowed_ids() -> set[str]:
    out: set[str] = set()
    for model_id in mp.DEFAULTS.values():
        out |= {model_id, mp.openclaw_ref(model_id), mp.openrouter_id(model_id)}
    return out


def drift(name: str, text: str, allowed: set[str], legacy=LEGACY) -> list[str]:
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _ID.finditer(line):
            token = m.group(0).rstrip(".-")
            if token in allowed or "anthropic/" + token in allowed or (name, token) in legacy:
                continue
            found.append(f"{name}:{lineno}: {token}")
    return found


def test_hand_written_ids_follow_the_declaration():
    allowed = allowed_ids()
    problems = []
    for name in SCANNED:
        problems += drift(name, (REPO / name).read_text(encoding="utf-8"), allowed)
    assert not problems, "Claude ids that are not model_pins.DEFAULTS renderings (fix them or add a LEGACY entry):\n" + "\n".join(problems)


def test_drift_is_detected_and_names_the_file():
    text = "classes:\n  opus: anthropic/claude-opus-4-7\n"
    assert drift("profiles/x.yaml", text, allowed_ids()) == ["profiles/x.yaml:2: claude-opus-4-7"]


def test_legacy_entries_still_exist():
    for (name, token), _reason in LEGACY.items():
        assert token in (REPO / name).read_text(encoding="utf-8"), f"stale LEGACY entry {name} {token}"
