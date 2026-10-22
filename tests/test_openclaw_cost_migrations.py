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


# --- ADR-0014 D5: workboard disable ----------------------------------------------------------------------------------

from ai_resources import openclaw_reload_rules as rr  # noqa: E402
from ai_resources.setup.cockpits import _openclaw_host as section  # noqa: E402
from reload_rules_fixture import make_rules_unavailable  # noqa: E402

WB = ("plugins", "entries", "workboard", "enabled")


def test_the_workboard_migration_is_proposed_only_when_the_plugin_is_enabled():
    on = {"plugins": {"entries": {"workboard": {"enabled": True}}}}
    got = {p.id: p for p in cm.proposals(on)}
    assert got["cost-workboard-disable"].path == WB and got["cost-workboard-disable"].new is False
    assert cm.WORKBOARD_DISABLE_ID == "cost-workboard-disable"
    assert "cost-workboard-disable" not in {p.id for p in cm.proposals({"plugins": {"entries": {"workboard": {"enabled": False}}}})}
    assert "cost-workboard-disable" not in {p.id for p in cm.proposals({})}
    assert "cost-workboard-disable" not in {p.id for p in cm.proposals(on, applied=["cost-workboard-disable"])}
    assert "cost-workboard-disable" not in {p.id for p in cm.proposals(on, keep=frozenset({"plugins.entries.workboard"}))}


def _wb_live(sim, enabled=True):
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc.setdefault("plugins", {}).setdefault("entries", {}).setdefault("workboard", {})["enabled"] = enabled
    sim.cfg.write_text(json.dumps(doc), encoding="utf-8")
    sim.overrides_file.unlink(missing_ok=True)


def _only(script, key, answer):
    script.answers["Replace "] = False                       # every other migration is declined
    script.answers[key] = answer


def test_confirming_the_workboard_disable_sets_it_hot_records_the_id_and_keeps_it_off(sim, script):
    _wb_live(sim)
    _only(script, "Replace plugins.entries.workboard.enabled", True)
    s = _state()
    _run_wizard(s)
    assert _cfg(sim)["plugins"]["entries"]["workboard"]["enabled"] is False
    assert "cost-workboard-disable" in s.openclaw.cost_migrations_applied
    assert any(c["path"] == list(WB) and c["previous"] is True and c["had"] is True for c in s.openclaw.host_config_changes)
    # the host section must not turn it back on, now or on the next run
    sim.oc.calls.clear()
    _run_wizard(s)
    assert not [a for a, _p in sim.oc.calls if a[:2] == ["plugins", "enable"]]
    assert _cfg(sim)["plugins"]["entries"]["workboard"]["enabled"] is False


def test_declining_the_workboard_disable_changes_and_records_nothing(sim, script):
    _wb_live(sim)
    _only(script, "Replace plugins.entries.workboard.enabled", False)
    s = _state()
    _run_wizard(s)
    assert _cfg(sim)["plugins"]["entries"]["workboard"]["enabled"] is True
    assert "cost-workboard-disable" not in s.openclaw.cost_migrations_applied
    assert any("workboard" in m and "config set" in m or "kit-host-overrides" in m for m in script.messages("detail"))


# --- ADR-0003: a restart-required (or unclassifiable) key is never applied unattended ----------------------------------

def _gateway_restarts(sim):
    return [c for c in sim.systemd.calls if c[:3] == ["systemctl", "--user", "stop"] or c[:3] == ["systemctl", "--user", "restart"]]


def test_unattended_never_applies_a_migration_even_a_hot_one(sim, script):
    _live(sim)
    _wb_live(sim)
    script.non_interactive = True
    s = _state()
    _run_wizard(s)
    assert [a for a, p in sim.oc.real_patches() if p and ("plugins" in p or "thinkingDefault" in json.dumps(p))] == []
    assert _cfg(sim)["plugins"]["entries"]["workboard"]["enabled"] is True
    assert s.openclaw.cost_migrations_applied == [] and s.openclaw.host_restart_pending == []
    assert _gateway_restarts(sim) == []


def test_a_confirmed_key_the_rules_cannot_classify_is_recorded_pending_and_not_applied(sim, script, monkeypatch):
    _wb_live(sim)
    make_rules_unavailable(monkeypatch)                      # fail closed: every key reads restart-required
    script.answers["Apply in a drained window"] = False      # the profile's own restart-required keys stay pending too
    _only(script, "Replace plugins.entries.workboard.enabled", True)
    s = _state()
    _run_wizard(s)
    assert _cfg(sim)["plugins"]["entries"]["workboard"]["enabled"] is True            # not applied
    pend = [e for e in s.openclaw.host_restart_pending if e.get("op") == "migration"]
    assert [(e["id"], e["path"], e["value"], e["previous"], e["had"]) for e in pend] == [
        ("cost-workboard-disable", "plugins.entries.workboard.enabled", False, True, True)]
    assert "cost-workboard-disable" not in s.openclaw.cost_migrations_applied
    assert _gateway_restarts(sim) == []
    assert any("apply-pending" in m for m in script.messages("info"))
    # recording it twice does not duplicate it
    _run_wizard(s)
    assert len([e for e in s.openclaw.host_restart_pending if e.get("op") == "migration"]) == 1


def test_apply_pending_applies_a_pending_migration_in_a_drained_window_and_records_its_id(sim, script, monkeypatch):
    _wb_live(sim)
    s = _state()
    s.openclaw.host_restart_pending = cm.pending_entries(
        [p for p in cm.proposals(_cfg(sim)) if p.id == "cost-workboard-disable"], at="2026-10-22T00:00:00+00:00")
    assert section.apply_pending(s, assume_yes=True) == 0
    assert _cfg(sim)["plugins"]["entries"]["workboard"]["enabled"] is False
    assert "cost-workboard-disable" in s.openclaw.cost_migrations_applied and s.openclaw.host_restart_pending == []
    assert any(c["path"] == list(WB) and c["previous"] is True for c in s.openclaw.host_config_changes)


def test_apply_pending_drops_a_migration_whose_path_the_operator_changed_since(sim, script):
    _wb_live(sim)
    s = _state()
    s.openclaw.host_restart_pending = cm.pending_entries(
        [p for p in cm.proposals(_cfg(sim)) if p.id == "cost-workboard-disable"], at="2026-10-22T00:00:00+00:00")
    _wb_live(sim, enabled="manual")                          # the operator set something else meanwhile
    sim.oc.calls.clear()
    assert section.apply_pending(s, assume_yes=True) == 0
    assert _cfg(sim)["plugins"]["entries"]["workboard"]["enabled"] == "manual"
    assert s.openclaw.cost_migrations_applied == [] and s.openclaw.host_restart_pending == []
    assert not sim.oc.real_patches()


def test_pending_entries_carry_no_secret_and_the_expected_shape():
    p = cm.Proposal("x", ("a", "b"), "old", "new", "why", replace=True)
    (e,) = cm.pending_entries([p], at="T")
    assert e == {"op": "migration", "id": "x", "path": "a.b", "value": "new", "previous": "old", "had": True,
                 "replace": True, "source": "setup", "at": "T"}
    q = cm.Proposal("y", ("a",), None, "0m", "why", had=False)
    assert cm.change_records([q]) == [{"path": ["a"], "previous": None, "had": False, "action": "filled"}]
    assert cm.change_records([p]) == [{"path": ["a", "b"], "previous": "old", "had": True, "action": "forced"}]
