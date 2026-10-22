"""Confirmed cost migrations (ADR-0003): old -> new per path, per-path confirm, ids recorded, nothing unattended."""
from __future__ import annotations

import json

import pytest

from ai_resources import openclaw_cost_migrations as cm
from ai_resources.setup import state
from ai_resources.setup.cockpits import openclaw
from test_openclaw_host_wizard import _run_wizard, _state, script, sim  # noqa: F401  (fixtures)

LIVE = {"agents": {
    "defaults": {"thinkingDefault": "medium",
                 "model": {"primary": "anthropic/claude-sonnet-5-5",
                           "fallbacks": ["anthropic/claude-opus-5-5", "anthropic/claude-haiku-5-5"]}},
    "entries": {
        "main": {"thinkingDefault": "high", "model": {"primary": "anthropic/claude-sonnet-5-5",
                 "fallbacks": ["anthropic/claude-opus-5-5", "anthropic/claude-haiku-5-5"]}},
        "opuslane": {"model": {"primary": "anthropic/claude-opus-5-5",
                               "fallbacks": ["anthropic/claude-sonnet-5-5"]}},
        "clean": {"model": {"primary": "anthropic/claude-sonnet-5-5", "fallbacks": ["anthropic/claude-haiku-5-5"]}},
    }}}


# --- the pure module -------------------------------------------------------------------------------------------

def test_proposals_cover_thinking_and_opus_fallbacks_only_where_they_differ():
    got = {p.id: p for p in cm.proposals(LIVE)}
    assert set(got) == {"cost-thinking-default-low", "cost-thinking-main-medium",
                        "cost-fallbacks-no-opus:defaults", "cost-fallbacks-no-opus:main"}
    assert (got["cost-thinking-main-medium"].old, got["cost-thinking-main-medium"].new) == ("high", "medium")
    assert got["cost-fallbacks-no-opus:main"].new == ["anthropic/claude-haiku-5-5"]
    assert got["cost-fallbacks-no-opus:main"].replace is True


def test_an_opus_primary_lane_keeps_its_fallbacks_and_is_not_listed():
    assert "opuslane" not in cm.opus_fallback_agents(LIVE)
    assert cm.opus_fallback_agents(LIVE) == ["defaults", "main"]


def test_the_keep_list_wins_and_applied_ids_are_not_proposed_again():
    keep = frozenset({"agents.entries.main"})
    ids = {p.id for p in cm.proposals(LIVE, keep=keep)}
    assert "cost-thinking-main-medium" not in ids and "cost-fallbacks-no-opus:main" not in ids
    done = ["cost-thinking-default-low", "cost-fallbacks-no-opus:defaults"]
    assert {p.id for p in cm.proposals(LIVE, applied=done)} == {"cost-thinking-main-medium", "cost-fallbacks-no-opus:main"}


def test_a_value_already_at_the_target_or_absent_is_not_a_migration():
    doc = {"agents": {"defaults": {"thinkingDefault": "low"}, "entries": {"main": {}}}}
    assert cm.proposals(doc) == []


def test_a_forced_path_is_left_to_the_normal_forced_fill():
    force = frozenset({"agents.defaults.thinkingDefault"})
    assert "cost-thinking-default-low" not in {p.id for p in cm.proposals(LIVE, force=force)}


def test_the_patch_replaces_lists_and_sets_values():
    props = [p for p in cm.proposals(LIVE) if p.id in ("cost-thinking-main-medium", "cost-fallbacks-no-opus:main")]
    patch, replace = cm.build_patch(props)
    assert patch["agents"]["entries"]["main"]["thinkingDefault"] == "medium"
    assert patch["agents"]["entries"]["main"]["model"]["fallbacks"] == ["anthropic/claude-haiku-5-5"]
    assert replace == ["agents.entries.main.model.fallbacks"]


def test_every_declined_path_has_a_copy_paste_snippet():
    assert all(p.snippet for p in cm.proposals(LIVE))
    fb = [p for p in cm.proposals(LIVE) if p.replace][0]
    assert fb.snippet.startswith("openclaw config set agents.") and "claude-opus" not in fb.snippet


# --- through the setup wizard ----------------------------------------------------------------------------------

def _live(sim):
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["agents"]["defaults"]["thinkingDefault"] = "medium"
    doc["agents"]["entries"]["main"]["thinkingDefault"] = "high"
    doc["agents"]["entries"]["main"]["model"] = {"primary": "anthropic/claude-sonnet-5-5",
                                                  "fallbacks": ["anthropic/claude-opus-5-5", "anthropic/claude-haiku-5-5"]}
    sim.cfg.write_text(json.dumps(doc), encoding="utf-8")
    sim.overrides_file.unlink()                      # fill-only: the operator's values are kept by the profile
    return doc


def _cfg(sim):
    return json.loads(sim.cfg.read_text(encoding="utf-8"))


def _ask_all(script, answer: bool):
    script.answers["Replace agents."] = answer


def test_confirming_replaces_the_values_and_records_the_ids(sim, script):
    _live(sim)
    _ask_all(script, True)
    s = _state()
    _run_wizard(s)
    doc = _cfg(sim)
    assert doc["agents"]["entries"]["main"]["thinkingDefault"] == "medium"
    assert doc["agents"]["defaults"]["thinkingDefault"] == "low"
    assert doc["agents"]["entries"]["main"]["model"]["fallbacks"] == ["anthropic/claude-haiku-5-5"]
    assert "cost-thinking-main-medium" in s.openclaw.cost_migrations_applied
    assert any(c["path"] == ["agents", "entries", "main", "thinkingDefault"] and c["previous"] == "high"
               for c in s.openclaw.host_config_changes)
    assert any("->" in m and '"high"' in m and '"medium"' in m for m in script.messages("detail"))


def test_a_rerun_after_confirming_proposes_nothing_and_changes_nothing(sim, script):
    _live(sim)
    _ask_all(script, True)
    s = _state()
    _run_wizard(s)
    before = sim.cfg.read_bytes()
    script.asked.clear()
    _run_wizard(s)
    assert not [q for q in script.asked if q.startswith("Replace agents.")]
    assert sim.cfg.read_bytes() == before


def test_declining_keeps_every_value_and_prints_the_snippet(sim, script):
    _live(sim)
    _ask_all(script, False)
    s = _state()
    _run_wizard(s)
    assert _cfg(sim)["agents"]["entries"]["main"]["thinkingDefault"] == "high"
    assert s.openclaw.cost_migrations_applied == []
    details = " | ".join(script.messages("detail"))
    assert "kit-host-overrides.json5" in details and "openclaw config set agents.entries.main.model.fallbacks" in details


def test_non_interactive_applies_nothing_and_asks_nothing(sim, script):
    _live(sim)
    script.non_interactive = True
    s = _state()
    _run_wizard(s)
    assert _cfg(sim)["agents"]["entries"]["main"]["thinkingDefault"] == "high"
    assert not [q for q in script.asked if q.startswith("Replace agents.")]
    assert s.openclaw.cost_migrations_applied == []
    assert any("Not applied (needs an interactive confirm)" in m for m in script.messages("info"))


def test_a_dry_run_applies_nothing(sim, script):
    _live(sim)
    before = sim.cfg.read_bytes()
    s = _state()
    _run_wizard(s, dry_run=True)
    assert sim.cfg.read_bytes() == before and s.openclaw.cost_migrations_applied == []


def test_the_keep_list_in_the_overrides_file_wins(sim, script):
    _live(sim)
    sim.overrides_file.write_text('{"keep": ["agents.entries.main"]}\n', encoding="utf-8")
    _ask_all(script, True)
    _run_wizard(_state())
    doc = _cfg(sim)
    assert doc["agents"]["entries"]["main"]["thinkingDefault"] == "high"
    assert doc["agents"]["defaults"]["thinkingDefault"] == "low"           # not under the keep entry


def test_an_opus_fallback_behind_a_non_opus_primary_is_warned_about(sim, script):
    _live(sim)
    script.non_interactive = True
    _run_wizard(_state())
    assert any("Opus fallback" in m and "main" in m for m in script.messages("warn"))


def test_a_rejected_dry_run_applies_nothing(sim, script, monkeypatch):
    _live(sim)
    _ask_all(script, True)
    before = sim.cfg.read_bytes()
    orig = sim.oc.__call__

    def reject(args, stdin=None, timeout=120):
        if args[:2] == ["config", "patch"] and "--dry-run" in args and stdin and "thinkingDefault" in stdin \
                and "fallbacks" in stdin:
            return 1, "invalid: schema"
        return orig(args, stdin, timeout)

    sim.oc.dry_run_ok = True
    monkeypatch.setattr(openclaw, "_openclaw", reject)
    s = _state()
    _run_wizard(s)
    assert s.openclaw.cost_migrations_applied == []


def test_the_state_file_omits_the_ids_until_one_is_applied_and_round_trips_them():
    s = state.SetupState()
    assert "cost_migrations_applied" not in state._to_dict(s)["openclaw"]
    s.openclaw.cost_migrations_applied.append("x")
    back = state._from_dict(state.SetupState, state._to_dict(s))
    assert back.openclaw.cost_migrations_applied == ["x"]
