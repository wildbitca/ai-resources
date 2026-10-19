"""ADR-0010 S9: the five-phase rail changes presentation only. A saved v1.0 state, the step ids and the
Edit/Back targets behave exactly as before; only the header numbering moves from n/4 to n/5."""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil

from ai_resources.setup import flow, state, ui, wizard
from wizard_fakes import Script, host_env

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "setup_state_v1_0.json"

STEP_IDS = ["tools", "mode", "gateway", "providers", "credentials", "preset", "models", "profile", "lanes",
            "cockpits", "lifecycle", "review", "apply"]


def _args(**kw):
    ns = dict(dry_run=True, yes=True, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
              shape="", providers="", smoke_path="", cockpits="")
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_step_ids_and_internal_numbers_are_unchanged():
    steps = wizard._build_steps(True)
    assert [s.id for s in steps] == STEP_IDS
    assert [s.number for s in steps] == [1, 2, 3, 4, 5, 6, 6, 6, 6, 7, 8, 9, 9]
    assert wizard.TOTAL_STEPS == 10 and len(flow.STEP_LABELS) == 10


def test_five_phases_and_their_labels():
    assert wizard.PHASES == 5 and len(flow.PHASE_LABELS) == 5
    assert flow.PHASE_LABELS == ("Tools", "Cockpit", "Team", "Audio", "More")
    assert {wizard._phase_of(n) for n in range(1, 11)} <= {1, 2, 3, 4, 5}
    assert wizard._phase_of(99) == 5                       # an unknown step belongs to the last phase


def test_edit_and_back_run_the_same_stepspec_for_every_step_id():
    """AC-S9b: the review's Edit labels point at ids that exist, whatever the phase numbering says."""
    ids = {s.id for s in wizard._build_steps(False)}
    for spec in wizard._build_steps(False):
        if spec.edit_label:
            assert spec.id in ids


def test_dry_run_yes_from_the_v1_0_fixture(monkeypatch, tmp_path):
    """AC-S9a/c: outputs and state equal the saved ones; every header reads n/5."""
    home = host_env(monkeypatch, tmp_path)
    (home / "xdg" / "ai-resources").mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, state.state_path())
    before = state.state_path().read_bytes()
    want = state._to_dict(state.load())
    seen = {}
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("s", s) and 0)
    headers = []
    real = ui.section
    monkeypatch.setattr(ui, "section", lambda n, t, title: (headers.append((n, t, title)), real(n, t, title))[1])
    script = Script(**{"Pick how they work": "advanced", "Use previous": True, "Select providers": ["openrouter"], "Update OPENROUTER": False,
                       "Starting team": lambda values: values[0], "Apply this?": lambda values: values[0]})
    script.install(monkeypatch)
    assert wizard.run(_args()) == 0
    assert headers and all(t == 5 and 1 <= n <= 5 for n, t, _ in headers)
    assert [n for n, _t, _ in headers] == sorted(n for n, _t, _ in headers)
    assert state.state_path().read_bytes() == before        # dry run: the saved file is not rewritten
    got = state._to_dict(seen["s"])
    assert got["mode"] == want["mode"] and got["backend"] == want["backend"]
    assert got["profile"]["name"] == want["profile"]["name"]
    assert re.fullmatch(r"\d+\.\d+", got["schema_version"]) and got["schema_version"] == want["schema_version"]
