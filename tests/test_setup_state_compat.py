"""Old setup-state files keep their meaning (ADR-0009, AC1).

The fixtures under tests/fixtures/setup_state/ are hand-built state files in the shape older kits wrote. Each is
loaded and saved without edits (the text must be byte-equal apart from `last_run`), and each is replayed through
`setup --non-interactive --dry-run`, which must leave mode, backend, cli_team, profile.*, lanes, lane_acks and
cockpits as they were. The state schema does not change in this feature; a failure here means it did."""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import types

import pytest

from ai_resources.setup import credentials, detection, state, tools, ui, wizard

FIX = pathlib.Path(__file__).parent / "fixtures" / "setup_state"
NAMES = sorted(p.stem for p in FIX.glob("*.yaml"))
# Nothing: a non-interactive run leaves every key of every fixture as it was (AC1). Two things used to drift and
# were fixed by ADR-0009: the single-model profile step rebuilt `profile` (dropping a customized or composed team
# and the lanes with their acknowledgements), and the OpenClaw prompt normalised `openclaw.model` and filled
# `voice_correction` on every run (it is now asked on the first configure only).
KNOWN_TODAY: dict[str, tuple[str, ...]] = {}
_LAST_RUN = re.compile(r"^last_run: .*$", re.M)


def test_the_fixture_matrix_is_complete():
    assert NAMES == sorted([
        "account_picked_selection", "cli_lanes_anthropic_ack", "cli_lanes_google_ack", "cli_team_balanced",
        "cli_team_customized", "composed_team", "gemini_cockpit", "multi_litellm", "multi_openrouter",
        "openclaw-pre-image", "openclaw_host_voice", "single_claude_native"])


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "xdg").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / "xdg"))
    return h


def _seed(name):
    state.config_root().mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / f"{name}.yaml", state.state_path())


@pytest.mark.parametrize("name", NAMES)
def test_load_then_save_is_byte_equal(home, name):
    _seed(name)
    before = state.state_path().read_text(encoding="utf-8")
    state.save(state.load())
    after = state.state_path().read_text(encoding="utf-8")
    assert _LAST_RUN.sub("", after) == _LAST_RUN.sub("", before)


@pytest.mark.parametrize("name", NAMES)
def test_non_interactive_dry_run_leaves_the_answers_unchanged(home, monkeypatch, name):
    _seed(name)
    want = state._to_dict(state.load())
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "detected_table", lambda rows: None)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: {
        cid: types.SimpleNamespace(installed=True, version="1.0", binary_path=f"/opt/{cid}", name=cid)
        for cid in want["cockpits"]})
    monkeypatch.setattr(detection, "detect_container_runtime",
                        lambda: ("docker", types.SimpleNamespace(installed=False, version="", binary_path="")))
    monkeypatch.setattr(tools, "offer", lambda *a, **k: [])
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "stored")
    monkeypatch.setattr(credentials, "get_claude_code_keychain_key", lambda: None)
    seen = {}
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("s", s) and 0)
    monkeypatch.setattr(wizard, "_step10_verify", lambda s, t: 0)
    monkeypatch.setattr(wizard, "_print_completion", lambda s: None)
    args = argparse.Namespace(dry_run=True, yes=False, non_interactive=True, profile="", lanes=False, no_lanes=False,
                              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    rc = wizard.run(args)
    assert rc == 0 and "s" in seen
    got = state._to_dict(seen["s"])
    a, b = _flat(want), _flat(got)
    drift = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    # The transient stash of the ticked tools is not part of a saved file, and `last_run` is stamped on save.
    drift = {k for k in drift if not k.startswith("profile.customizations.__targets__") and k != "last_run"}
    allowed = [pfx for pfx in KNOWN_TODAY.get(name, ())]
    unexplained = sorted(k for k in drift if not any(k == p or k.startswith(p + ".") for p in allowed))
    assert not unexplained, unexplained


def _flat(doc, prefix=""):
    if not isinstance(doc, dict):
        return {prefix: doc}
    out = {}
    for k, v in doc.items():
        out.update(_flat(v, f"{prefix}.{k}" if prefix else k))
    return out
