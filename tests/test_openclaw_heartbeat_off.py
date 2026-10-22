"""ADR-0014 D5: the main heartbeat stays as it is unless the operator says "off"; "off" is a fill when the key is unset
and a confirmed, recorded migration when the host already holds another value."""
from __future__ import annotations

import json

from ai_resources import openclaw_cost_migrations as cm
from ai_resources import openclaw_host as host
from ai_resources.setup import state
from test_openclaw_host_wizard import ANSWERS, _run_wizard, _state, script, sim  # noqa: F401  (fixtures)

PATH = ("agents", "entries", "main", "heartbeat", "every")


def _cfg(sim):
    return json.loads(sim.cfg.read_text(encoding="utf-8"))


def _every(doc):
    return (((doc.get("agents") or {}).get("entries") or {}).get("main") or {}).get("heartbeat", {}).get("every")


def _set_every(sim, value):
    doc = _cfg(sim)
    hb = doc["agents"]["entries"]["main"].setdefault("heartbeat", {})
    if value is None:
        hb.pop("every", None)
    else:
        hb["every"] = value
    sim.cfg.write_text(json.dumps(doc), encoding="utf-8")
    sim.overrides_file.unlink(missing_ok=True)


def _decline_the_rest(script, heartbeat_answer):
    script.answers["Replace "] = False
    script.answers["Turn the main agent's heartbeat off"] = heartbeat_answer


# --- the pure proposal -------------------------------------------------------------------------------------------------

def test_no_proposal_without_the_operators_answer():
    doc = {"agents": {"entries": {"main": {"heartbeat": {"every": "2h"}}}}}
    assert "cost-heartbeat-off" not in {p.id for p in cm.proposals(doc)}
    (p,) = [p for p in cm.proposals(doc, heartbeat_off=True) if p.id == "cost-heartbeat-off"]
    assert (p.path, p.old, p.new, p.had) == (PATH, "2h", "0m", True)


def test_an_unset_key_is_a_fill_and_a_value_already_off_is_nothing():
    (p,) = [p for p in cm.proposals({}, heartbeat_off=True) if p.id == "cost-heartbeat-off"]
    assert p.had is False and p.old is None
    assert cm.change_records([p])[0]["action"] == "filled"
    done = {"agents": {"entries": {"main": {"heartbeat": {"every": "0m"}}}}}
    assert "cost-heartbeat-off" not in {q.id for q in cm.proposals(done, heartbeat_off=True)}
    assert "cost-heartbeat-off" not in {q.id for q in cm.proposals({}, heartbeat_off=True, applied=["cost-heartbeat-off"])}
    keep = frozenset({"agents.entries.main.heartbeat"})
    assert "cost-heartbeat-off" not in {q.id for q in cm.proposals({}, heartbeat_off=True, keep=keep)}


def test_the_profile_fill_follows_the_answer_and_stays_fill_only():
    profile = host.load_host_profile()
    base = {"DOMAIN": "ai.example.org", "POD_CIDR": "10.9.0.0/24", "HOME": "/home/u", "TAILNET": "1"}
    default = host.build_host_patch(profile, {}, base)
    assert default["patch"]["agents"]["entries"]["main"]["heartbeat"]["every"] == "2h"
    off = host.build_host_patch(profile, {}, {**base, "MAIN_HEARTBEAT_EVERY": "0m"})
    assert off["patch"]["agents"]["entries"]["main"]["heartbeat"]["every"] == "0m"
    held = {"agents": {"entries": {"main": {"heartbeat": {"every": "6h"}}}}}
    kept = host.build_host_patch(profile, held, {**base, "MAIN_HEARTBEAT_EVERY": "0m"})
    assert "every" not in ((kept["patch"].get("agents") or {}).get("entries", {}).get("main", {}).get("heartbeat") or {})


def test_the_state_flag_is_omitted_while_false_and_round_trips():
    s = state.SetupState()
    assert "host_heartbeat_off" not in state._to_dict(s)["openclaw"]
    s.openclaw.host_heartbeat_off = True
    assert state._from_dict(state.SetupState, state._to_dict(s)).openclaw.host_heartbeat_off is True


# --- through the wizard ------------------------------------------------------------------------------------------------

def test_the_default_answer_keeps_the_two_hour_heartbeat(sim, script):
    _set_every(sim, "2h")
    _decline_the_rest(script, False)
    s = _state()
    _run_wizard(s)
    assert _every(_cfg(sim)) == "2h" and s.openclaw.host_heartbeat_off is False
    assert not [q for q in script.asked if "Replace agents.entries.main.heartbeat.every" in q]


def test_yes_and_a_confirm_replaces_the_value_and_records_the_id_and_the_old_value(sim, script):
    _set_every(sim, "2h")
    _decline_the_rest(script, True)
    script.answers["Replace agents.entries.main.heartbeat.every"] = True
    s = _state()
    _run_wizard(s)
    assert _every(_cfg(sim)) == "0m" and s.openclaw.host_heartbeat_off is True
    assert "cost-heartbeat-off" in s.openclaw.cost_migrations_applied
    assert any(c["path"] == list(PATH) and c["previous"] == "2h" for c in s.openclaw.host_config_changes)
    before = sim.cfg.read_bytes()
    script.asked.clear()
    _run_wizard(s)                                          # idempotent: nothing is proposed again
    assert not [q for q in script.asked if "heartbeat.every" in q and q.startswith("Replace")]
    assert sim.cfg.read_bytes() == before


def test_yes_but_declining_the_replacement_keeps_the_hosts_value(sim, script):
    _set_every(sim, "2h")
    _decline_the_rest(script, True)
    script.answers["Replace agents.entries.main.heartbeat.every"] = False
    s = _state()
    _run_wizard(s)
    assert _every(_cfg(sim)) == "2h" and "cost-heartbeat-off" not in s.openclaw.cost_migrations_applied
    assert any("openclaw config set agents.entries.main.heartbeat.every 0m" in m for m in script.messages("detail"))


def test_yes_with_the_key_unset_fills_it_without_a_replace_question(sim, script):
    _set_every(sim, None)
    _decline_the_rest(script, True)
    script.answers["Replace agents.entries.main.heartbeat.every"] = True
    s = _state()
    _run_wizard(s)
    assert _every(_cfg(sim)) == "0m"


def test_unattended_never_changes_the_heartbeat(sim, script):
    _set_every(sim, "2h")
    script.non_interactive = True
    s = _state()
    s.openclaw.host_heartbeat_off = True                     # an earlier interactive answer
    _run_wizard(s)
    assert _every(_cfg(sim)) == "2h" and "cost-heartbeat-off" not in s.openclaw.cost_migrations_applied
