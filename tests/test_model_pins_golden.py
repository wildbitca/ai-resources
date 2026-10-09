"""Golden snapshots of every OpenClaw-path model declaration, taken BEFORE the single-declaration refactor.

The fixtures under tests/fixtures/model_pins_golden/ were generated from the code as it stood before
`model_pins.py` existed. The refactor must keep them byte-identical (with no overlay on the host).
Regenerate deliberately with:  MODEL_PINS_GOLDEN_UPDATE=1 pytest tests/test_model_pins_golden.py
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import audit, model_pins  # noqa: E402
from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup import state  # noqa: E402
from ai_resources.setup.cockpits import openclaw  # noqa: E402

GOLDEN = REPO / "tests" / "fixtures" / "model_pins_golden"
HOST_FIXTURE = REPO / "tests" / "fixtures" / "openclaw_host_config.json"
VALUES = {"DOMAIN": "ai.example.org", "POD_CIDR": "10.9.0.0/24", "HOME": "/home/u"}


def _engine(e) -> dict:
    return {"id": e.id, "runtime": e.runtime, "requires": e.requires, "models": list(e.models),
            "disabled_reason": e.disabled_reason}


def _worker_model_literals() -> list[str]:
    src = (REPO / "scripts/ai_resources/setup/cockpits/openclaw.py").read_text(encoding="utf-8")
    # The literal fallback now comes from model_pins (default_worker_model()): resolve it so the
    # golden stays the same list of bare ids.
    found = re.findall(r'worker_model or (?:"([^"]+)"|default_worker_model\(\))', src)
    return [f or openclaw.default_worker_model() for f in found]


def snapshots(tmp_home: pathlib.Path) -> dict[str, object]:
    profile = host.load_host_profile()
    doc = json.loads(HOST_FIXTURE.read_text(encoding="utf-8"))
    # FORCE_ALL: the snapshot pins what the profile says against a customised host (fill-only is tested elsewhere).
    built = host.build_host_patch(profile, doc, VALUES, overrides=host.FORCE_ALL)
    return {
        "audit.json": {"prices": {k: list(v) for k, v in audit.PRICES.items()}, "aliases": dict(audit.ALIASES)},
        "engines.json": {"claude-code": _engine(openclaw.ENGINES["claude-code"]),
                         "antigravity": _engine(openclaw.ENGINES["antigravity"])},
        "worker_model.json": {"state_default": state.OpenClawState().worker_model,
                              "cockpit_fallbacks": _worker_model_literals()},
        # The sonnet literal in the profile is now the @MODEL_SONNET@ marker; resolve it to compare.
        "host_profile.json": json.loads(json.dumps(profile).replace(
            "@MODEL_SONNET@", model_pins.DEFAULTS["sonnet"])),
        "host_patch.json": {"patch": built["patch"], "replace_paths": built["replace_paths"],
                            "skipped": built["skipped"]},
    }


@pytest.fixture(autouse=True)
def _no_overlay_home(monkeypatch, tmp_path):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(host.Path, "home", classmethod(lambda cls: tmp_path))


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("name", ["audit.json", "engines.json", "worker_model.json",
                                  "host_profile.json", "host_patch.json"])
def test_golden_snapshot_is_unchanged(name, tmp_path):
    got = _dump(snapshots(tmp_path)[name])
    path = GOLDEN / name
    if os.environ.get("MODEL_PINS_GOLDEN_UPDATE"):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(got, encoding="utf-8")
    assert path.exists(), f"missing golden {path}; generate it with MODEL_PINS_GOLDEN_UPDATE=1"
    assert got == path.read_text(encoding="utf-8")
