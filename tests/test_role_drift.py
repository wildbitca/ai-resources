"""Matrix vs live drift: `role_drift.check`, its `verify --live` cases, and the `build_patch` reconcile.

Generated config only: every document here is a dict handed to pure functions. Nothing calls openclaw, claude, agy or systemd.
"""
from __future__ import annotations

import copy
from pathlib import Path

from ai_resources import acceptance as acc
from ai_resources import repo_root, role_drift
from ai_resources.scoring import routing
from ai_resources.setup import effort, state
from ai_resources.setup.cockpits import openclaw_roles as R

EFFECTIVE = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
CATALOG = {"anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5")}
PIN = {"agentRuntime": {"id": "claude-cli"}}


def _chain(role):
    return routing.openclaw_chain(routing.ROLES[role], EFFECTIVE)


def _model(role):
    primary, fallbacks = _chain(role)
    return {"primary": primary, "fallbacks": list(fallbacks)}


def _doc(**entries):
    return {"agents": {"defaults": {}, "entries": entries}}


def _agents():
    s = state.SetupState()
    s.profile.name = "balanced"
    out, _ = R.build_agents(repo_root(), enabled=("anthropic",), catalog=CATALOG,
                            effort_table=effort.table_for_state(s), ak_path="/kit", by_persona={}, env={},
                            effective=EFFECTIVE)
    return [a for a in out if a.id in ("kit-role-explore", "kit-role-planner")]


def _ws(agents):
    return {a.id: Path("/ws") / a.id for a in agents}


# --- the check -----------------------------------------------------------------------------------------------------

def test_a_host_equal_to_the_matrix_has_no_drift():
    doc = _doc(**{"kit-role-explore": {"model": _model("explore"), "subagents": {"model": _model("explore")}},
                  "kit-role-planner": {"model": _model("planner"), "models": {_chain("planner")[0]: PIN}}})
    assert role_drift.check(doc, effective=EFFECTIVE) == []


def test_a_role_still_on_claude_while_the_matrix_says_agy_is_an_error_naming_role_expected_actual():
    doc = _doc(**{"kit-role-explore": {"model": {"primary": "anthropic/claude-sonnet-5-5"}}})
    found = role_drift.mismatches(role_drift.check(doc, effective=EFFECTIVE))
    primary = next(d for d in found if d.field == "model.primary")
    assert (primary.role, primary.expected, primary.actual) == ("explore", _chain("explore")[0], "anthropic/claude-sonnet-5-5")
    assert primary.agent_id in primary.line() and "explore" in primary.line()
    assert any(d.field == "model.fallbacks" for d in found)


def test_the_bare_string_model_form_is_compared_on_its_primary():
    primary, fallbacks = _chain("explore")
    doc = _doc(**{"kit-role-explore": {"model": primary}})
    found = role_drift.check(doc, effective=EFFECTIVE)
    assert [d.field for d in found] == ["model.fallbacks"] and found[0].expected == ", ".join(fallbacks)


def test_the_spawn_model_is_compared_including_the_host_default():
    doc = _doc(**{"kit-role-explore": {"model": _model("explore")}})
    doc["agents"]["defaults"]["subagents"] = {"model": {"primary": "anthropic/claude-sonnet-5-5"}}
    found = role_drift.check(doc, effective=EFFECTIVE)
    assert [d.field for d in found] == ["subagents.model"] and found[0].role == "explore"


def test_an_agent_that_is_not_in_the_document_is_skipped():
    assert role_drift.check(_doc(main={}), effective=EFFECTIVE) == []


def test_a_max_primary_on_the_anthropic_api_is_a_billing_pool_warning_not_a_mismatch():
    primary = _chain("planner")[0]
    doc = _doc(**{"kit-role-planner": {"model": _model("planner")}})          # same ref, no claude-cli pin
    found = role_drift.check(doc, effective=EFFECTIVE)
    assert role_drift.mismatches(found) == []
    (pool,) = role_drift.billing(found)
    assert pool.kind == role_drift.BILLING_POOL and pool.role == "planner" and primary in pool.actual
    assert pool.line().startswith("billing-pool mismatch")


def test_a_runtime_pin_in_the_agent_or_in_the_defaults_clears_the_billing_warning():
    primary = _chain("planner")[0]
    own = _doc(**{"kit-role-planner": {"model": _model("planner"), "models": {primary: PIN}}})
    dflt = _doc(**{"kit-role-planner": {"model": _model("planner")}})
    dflt["agents"]["defaults"]["models"] = {primary: PIN}
    assert role_drift.billing(role_drift.check(own, effective=EFFECTIVE)) == []
    assert role_drift.billing(role_drift.check(dflt, effective=EFFECTIVE)) == []


def test_a_profile_override_moves_the_expected_cell():
    profile = {"routing": {"explore": {"primary": "claude-code:haiku"}}}
    doc = _doc(**{"kit-role-explore": {"model": _model("explore")}})
    found = role_drift.mismatches(role_drift.check(doc, profile, EFFECTIVE))
    assert found and found[0].expected == "anthropic/claude-haiku-5-5"


# --- the verify --live cases ---------------------------------------------------------------------------------------

def _env(doc):
    return acc.Env(state=state.SetupState(), doc=lambda: doc, offline=True)


def _case(cid):
    return next(c for c in acc.CASES if c.id == cid)


def test_the_cases_are_registered_in_setup_live_with_the_right_severity():
    assert _case("setup.role-matrix-drift").group == "setup-live" and _case("setup.role-matrix-drift").severity == "error"
    assert _case("setup.role-billing-pool").group == "setup-live" and _case("setup.role-billing-pool").severity == "warn"


def test_the_drift_case_fails_with_every_mismatch_named_and_passes_when_equal():
    bad_doc = _doc(**{"kit-role-explore": {"model": {"primary": "anthropic/claude-sonnet-5-5"}}})
    r = acc.run_case(_case("setup.role-matrix-drift"), _env(bad_doc))
    assert r.status == acc.FAIL and "explore" in r.reason and "expected" in r.reason and "actual" in r.reason
    good = _doc(**{"kit-role-explore": {"model": _model("explore"), "subagents": {"model": _model("explore")}}})
    assert acc.run_case(_case("setup.role-matrix-drift"), _env(good)).status == acc.PASS


def test_the_cases_skip_a_host_without_kit_role_agents():
    for cid in ("setup.role-matrix-drift", "setup.role-billing-pool"):
        assert acc.run_case(_case(cid), _env(_doc(main={}))).status == acc.SKIP


# --- build_patch reconcile -----------------------------------------------------------------------------------------

def test_an_agent_not_in_created_agent_ids_is_moved_to_its_matrix_row_model_fields_only():
    agents = _agents()
    live = {"model": {"primary": "anthropic/claude-sonnet-5-5"}, "thinkingDefault": "high",
            "skills": ["mine"], "tools": {"deny": ["write"]}, "description": "operator text"}
    doc = _doc(**{"kit-role-explore": copy.deepcopy(live)})
    reconciled: list[str] = []
    patch, kept = R.build_patch([a for a in agents if a["role"] == "explore"], doc, _ws(agents), {}, {}, (), reconciled)
    ent = patch["agents"]["entries"]["kit-role-explore"]
    assert reconciled == ["kit-role-explore"] and kept == []
    assert ent["model"] == {"primary": _chain("explore")[0], "fallbacks": list(_chain("explore")[1])}
    assert set(ent) <= {"model", "subagents"}, "no other operator-owned field is touched"


def test_the_spawn_model_is_reconciled_only_where_the_operator_set_one():
    (agent,) = [a for a in _agents() if a["role"] == "explore"]
    doc = _doc(**{"kit-role-explore": {"model": {"primary": "anthropic/claude-sonnet-5-5"},
                                       "subagents": {"model": {"primary": "anthropic/claude-sonnet-5-5"}}}})
    patch, _ = R.build_patch([agent], doc, _ws([agent]), {}, {}, (), [])
    assert patch["agents"]["entries"]["kit-role-explore"]["subagents"]["model"] == R._model_value(agent)


def test_a_matching_agent_outside_created_agent_ids_is_left_alone():
    (agent,) = [a for a in _agents() if a["role"] == "explore"]
    doc = _doc(**{"kit-role-explore": {"model": R._model_value(agent), "thinkingDefault": "xhigh"}})
    reconciled: list[str] = []
    patch, _ = R.build_patch([agent], doc, _ws([agent]), {}, {}, (), reconciled)
    assert reconciled == [] and "model" not in patch.get("agents", {}).get("entries", {}).get("kit-role-explore", {})


def test_a_recorded_agent_the_operator_changed_is_still_kept():
    (agent,) = [a for a in _agents() if a["role"] == "explore"]
    doc = _doc(**{"kit-role-explore": {"model": {"primary": "anthropic/claude-opus-5-5"}}})
    recorded = {"created_agent_ids": ["kit-role-explore"], "models": {"kit-role-explore": "anthropic/claude-sonnet-5-5"}}
    reconciled: list[str] = []
    patch, kept = R.build_patch([agent], doc, _ws([agent]), recorded, {}, (), reconciled)
    assert kept == ["kit-role-explore"] and reconciled == []
    assert "model" not in patch.get("agents", {}).get("entries", {}).get("kit-role-explore", {})


def test_reconcile_diff_names_each_changed_field():
    (agent,) = [a for a in _agents() if a["role"] == "explore"]
    lines = R.reconcile_diff("kit-role-explore", {"model": {"primary": "anthropic/claude-sonnet-5-5"}}, agent)
    assert any("model.primary anthropic/claude-sonnet-5-5 ->" in ln for ln in lines)
    assert any("model.fallbacks" in ln for ln in lines)
