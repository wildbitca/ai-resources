"""ADR-0014 D4 / AC-14: the Haiku notification route has its own guard, is fill-only, confirmed per channel, recorded,
removed by teardown without touching anything it did not write, and never applied when a key needs a restart."""
from __future__ import annotations

import copy
import json

import pytest

from ai_resources import openclaw_host as host
from ai_resources import openclaw_notification_route as nr
from ai_resources import openclaw_reload_rules as rr
from ai_resources import openclaw_slack_cost as sc
from ai_resources.setup import state, ui
from ai_resources.setup.cockpits import _openclaw_host as section
from reload_rules_fixture import make_rules_unavailable
from test_openclaw_host_wizard import FakeOpenclaw, ScriptedUi  # noqa: F401

HAIKU = "anthropic/claude-haiku-5-5"
CATCH_ALL = {"type": "route", "agentId": "main", "comment": "catch-all for slack: preserved, must stay LAST",
             "match": {"channel": "slack", "accountId": "*"}}
OPS = {"type": "route", "agentId": "devops", "comment": "DevOps Slack channel",
       "match": {"channel": "slack", "peer": {"kind": "channel", "id": "C0OPS"}}}
TG = {"type": "route", "agentId": "ai", "comment": "AI group", "match": {"channel": "telegram",
                                                                         "peer": {"kind": "group", "id": "-5576979230"}}}
DOC = {
    "agents": {"entries": {"main": {"model": {"primary": "anthropic/claude-sonnet-5-5"}}, "devops": {}}},
    "channels": {"slack": {"enabled": True, "channels": {c: {"requireMention": True} for c in
                                                         ("C0OPS", "C1N", "C2N", "C3N")}}},
    "bindings": [OPS, TG, CATCH_ALL],
}


# --- the guard -------------------------------------------------------------------------------------------------------

def _plan(channels=("C1N", "C2N"), doc=None):
    return nr.build_plan(doc or DOC, list(channels), HAIKU)


def test_a_plan_passes_the_guard_and_touches_only_the_agent_and_bindings():
    plan = _plan()
    assert set(plan.patch) == {"agents", "bindings"} and plan.replace_paths == ["bindings"]
    assert plan.patch["agents"] == {"entries": {nr.AGENT_ID: {"model": {"primary": HAIKU, "fallbacks": []}}}}
    nr.assert_notification_route_safe(plan.patch, plan.replace_paths, DOC["bindings"])


@pytest.mark.parametrize("mutate,why", [
    (lambda p: p.update({"tools": {"deny": ["x"]}}), "tools"),
    (lambda p: p.update({"channels": {"slack": {"allowFrom": ["U1"]}}}), "channels"),
    (lambda p: p["agents"]["entries"].update({"other": {"model": {"primary": HAIKU}}}), "second agent"),
    (lambda p: p["agents"]["entries"][nr.AGENT_ID].update({"tools": {"profile": "full"}}), "agent tools"),
    (lambda p: p["agents"]["entries"][nr.AGENT_ID]["model"].update({"primary": "anthropic/claude-sonnet-5-5"}), "non-haiku"),
    (lambda p: p["agents"]["entries"][nr.AGENT_ID]["model"].update({"fallbacks": ["anthropic/claude-opus-5-5"]}), "fallbacks"),
    (lambda p: p["agents"]["entries"].update({nr.AGENT_ID: None}), "delete outside a teardown"),
    (lambda p: p["bindings"][0].update({"agentId": "elsewhere"}), "operator entry edited"),
    (lambda p: p["bindings"].reverse(), "operator order changed"),
    (lambda p: p["bindings"].pop(0), "operator entry dropped"),
    (lambda p: p["bindings"][1].update({"agentId": nr.AGENT_ID, "match": {"channel": "telegram", "peer": {"kind": "group", "id": "-1"}}}), "non-slack route"),
])
def test_the_guard_refuses_everything_else(mutate, why):
    plan = _plan()
    patch = copy.deepcopy(plan.patch)
    mutate(patch)
    with pytest.raises(ValueError):
        nr.assert_notification_route_safe(patch, plan.replace_paths, DOC["bindings"])


@pytest.mark.parametrize("rp", [["agents"], ["bindings", "agents.entries"], ["channels.slack"], ["tools.deny"]])
def test_the_guard_refuses_other_replace_paths(rp):
    with pytest.raises(ValueError):
        nr.assert_notification_route_safe(_plan().patch, rp, DOC["bindings"])


def test_the_guard_refuses_a_new_route_over_an_existing_peer_and_a_bindings_patch_without_replace():
    patch = copy.deepcopy(_plan().patch)
    patch["bindings"].insert(0, nr.route_entry("C0OPS"))
    with pytest.raises(ValueError):
        nr.assert_notification_route_safe(patch, ["bindings"], DOC["bindings"])
    with pytest.raises(ValueError):
        nr.assert_notification_route_safe({"bindings": []}, [], DOC["bindings"])


def test_the_slack_writer_and_the_channels_guard_are_unchanged_by_this_module():
    with pytest.raises(ValueError):
        host.assert_channels_safe({"channels": {"slack": {"channels": {"C1": {"allowBots": "mentions"}}}}})
    with pytest.raises(ValueError):
        sc.assert_slack_cost_safe({"bindings": []})


# --- fill-only plans -------------------------------------------------------------------------------------------------

def test_routes_go_before_the_catch_all_and_every_other_entry_is_untouched():
    plan = _plan()
    new = plan.patch["bindings"]
    assert new[:2] == [OPS, TG] and new[-1] == CATCH_ALL
    assert [e["match"]["peer"]["id"] for e in new if e.get("agentId") == nr.AGENT_ID] == ["C1N", "C2N"]
    assert all(e["agentId"] == nr.AGENT_ID for e in new[2:4])
    assert DOC["bindings"] == [OPS, TG, CATCH_ALL]                      # the live document was not mutated


def test_a_channel_that_already_has_a_route_is_reported_and_left_alone():
    plan = _plan(("C0OPS", "C1N"))
    assert plan.channels == ["C1N"]
    assert any(n.startswith("C0OPS: already routed to agent devops") for n in plan.skipped)
    assert [e for e in plan.patch["bindings"] if e.get("agentId") == "devops"] == [OPS]


def test_nothing_to_add_means_no_patch_and_no_agent():
    plan = _plan(("C0OPS",))
    assert plan.patch == {} and plan.channels == [] and plan.agent_created is False
    assert _plan(()).patch == {}


def test_an_existing_haiku_agent_is_reused_not_rewritten():
    doc = copy.deepcopy(DOC)
    doc["agents"]["entries"][nr.AGENT_ID] = {"model": {"primary": HAIKU}, "workspace": "/mine"}
    plan = nr.build_plan(doc, ["C1N"], HAIKU)
    assert plan.agent_created is False and "agents" not in plan.patch and plan.patch["bindings"]


def test_an_existing_agent_that_is_not_on_haiku_is_never_routed_to():
    doc = copy.deepcopy(DOC)
    doc["agents"]["entries"][nr.AGENT_ID] = {"model": {"primary": "anthropic/claude-opus-5-5"}}
    plan = nr.build_plan(doc, ["C1N"], HAIKU)
    assert plan.patch == {} and any("not on Haiku" in n for n in plan.skipped)


def test_an_invalid_channel_id_is_skipped():
    plan = _plan(("../x", "C1N"))
    assert plan.channels == ["C1N"] and any("not a Slack channel id" in n for n in plan.skipped)


def test_labels_use_the_binding_comment_when_there_is_one():
    labels = nr.slack_channel_labels(DOC)
    assert labels["C0OPS"] == "C0OPS (DevOps Slack channel)" and labels["C1N"] == "C1N"


# --- through the wizard section --------------------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps(copy.deepcopy(DOC)))
    oc = FakeOpenclaw(cfg)
    fake = ScriptedUi()
    fake.install(monkeypatch)
    fake.picked = ["C1N", "C2N"]
    monkeypatch.setattr(ui, "checkbox", lambda msg, choices, default=None, **k: (fake.asked.append(msg), fake.picked)[1])
    fake.answers = {"Route Slack notification channels": True}
    o = state.OpenClawState()
    o.host_config = True
    return cfg, oc, fake, o


def _apply(oc):
    def apply_patch(patch, *, dry_run=False, replace_paths=None):
        if not dry_run:
            rr_paths = rr.restart_paths(patch, ruleset=rr.current_ruleset(lambda argv, **kw: (1, "")), reload_mode=None)
            if rr_paths:
                raise rr.RestartRequired(rr_paths)
        args = ["config", "patch", "--stdin"] + (["--dry-run"] if dry_run else [])
        for rp in replace_paths or []:
            args += ["--replace-path", rp]
        rc, out = oc(args, stdin=json.dumps(patch))
        return rc == 0, out
    return apply_patch


def _offer(env, **kw):
    cfg, oc, fake, o = env
    return section._offer_notification_route(o, json.loads(cfg.read_text()), cfg, kw.pop("written", []),
                                             dry_run=kw.pop("dry_run", False), apply_patch=_apply(oc),
                                             oc=oc)


def test_two_selected_channels_get_two_routes_to_the_haiku_agent(env):
    cfg, oc, fake, o = env
    written: list = []
    assert _offer(env, written=written) is True
    live = json.loads(cfg.read_text())
    routes = [e for e in live["bindings"] if e.get("agentId") == nr.AGENT_ID]
    assert [e["match"]["peer"]["id"] for e in routes] == ["C1N", "C2N"]
    assert live["bindings"][-1] == CATCH_ALL and live["bindings"][:2] == [OPS, TG]
    assert live["agents"]["entries"][nr.AGENT_ID]["model"] == {"primary": HAIKU, "fallbacks": []}
    assert live["agents"]["entries"]["main"] == DOC["agents"]["entries"]["main"]            # nothing else moved
    assert live["channels"] == DOC["channels"] and "tools" not in live
    assert [r["kind"] for r in o.notification_route_changes] == ["agent", "binding", "binding"] and written == [cfg]
    assert not [c for c in oc.calls if c[0][:1] == ["plugins"]]


def test_default_is_no_and_declining_changes_and_records_nothing(env):
    cfg, oc, fake, o = env
    fake.answers = {"Route Slack notification channels": False}
    before = cfg.read_bytes()
    assert _offer(env) is False
    assert cfg.read_bytes() == before and o.notification_route_changes == [] and not oc.real_patches()


def test_a_channel_left_unticked_gets_no_route(env):
    cfg, oc, fake, o = env
    fake.picked = ["C2N"]
    _offer(env)
    ids = [e["match"]["peer"]["id"] for e in json.loads(cfg.read_text())["bindings"] if e.get("agentId") == nr.AGENT_ID]
    assert ids == ["C2N"]
    fake.picked = []
    before = cfg.read_bytes()
    assert _offer(env) is False and cfg.read_bytes() == before


def test_a_rerun_asks_only_about_the_channels_still_without_a_route(env):
    cfg, oc, fake, o = env
    _offer(env)
    fake.asked.clear()
    fake.picked = ["C3N"]
    _offer(env)
    routes = [e["match"]["peer"]["id"] for e in json.loads(cfg.read_text())["bindings"] if e.get("agentId") == nr.AGENT_ID]
    assert routes == ["C1N", "C2N", "C3N"]
    assert len([r for r in o.notification_route_changes if r["kind"] == "agent"]) == 1      # the agent is created once
    fake.asked.clear()
    fake.picked = []
    assert _offer(env) is False                                                             # all routed now: no question
    assert not [q for q in fake.asked if q.startswith("Route Slack")]


def test_non_interactive_and_dry_runs_write_nothing_and_ask_nothing(env):
    cfg, oc, fake, o = env
    before = cfg.read_bytes()
    fake.non_interactive = True
    assert _offer(env) is False
    fake.non_interactive = False
    assert _offer(env, dry_run=True) is False
    assert cfg.read_bytes() == before and not fake.asked and not oc.real_patches()
    assert any("no route yet" in m for m in fake.messages("detail"))


def test_a_rejected_dry_run_applies_nothing(env):
    cfg, oc, fake, o = env
    oc.dry_run_ok = False
    before = cfg.read_bytes()
    assert _offer(env) is False and cfg.read_bytes() == before and o.notification_route_changes == []


def test_slack_off_or_absent_does_nothing(env):
    cfg, oc, fake, o = env
    assert section._offer_notification_route(o, {}, cfg, [], dry_run=False, apply_patch=_apply(oc)) is False
    off = {"channels": {"slack": {"enabled": False, "channels": {"C1N": {}}}}}
    assert section._offer_notification_route(o, off, cfg, [], dry_run=False, apply_patch=_apply(oc)) is False
    assert not fake.asked


def test_every_patch_sent_passes_the_guard(env):
    cfg, oc, fake, o = env
    _offer(env)
    for args, patch in oc.calls:
        if patch and args[:2] == ["config", "patch"]:
            nr.assert_notification_route_safe(patch, [args[i + 1] for i, a in enumerate(args) if a == "--replace-path"],
                                              DOC["bindings"])


# --- restart-required: print-only plus host_restart_pending ------------------------------------------------------------

def test_a_key_that_needs_a_restart_is_recorded_pending_and_never_applied(env, monkeypatch):
    cfg, oc, fake, o = env
    make_rules_unavailable(monkeypatch)                                  # fail closed: every key reads restart-required
    before = cfg.read_bytes()
    assert _offer(env) is False
    assert cfg.read_bytes() == before and not oc.real_patches() and o.notification_route_changes == []
    (entry,) = [e for e in o.host_restart_pending if e.get("op") == "route"]
    assert entry["channels"] == ["C1N", "C2N"] and entry["path"] == "bindings" and entry["source"] == "setup"
    assert any("apply-pending" in m for m in fake.messages("info"))
    fake.picked = ["C3N"]                                                # a second ask extends the same entry
    _offer(env)
    (entry,) = [e for e in o.host_restart_pending if e.get("op") == "route"]
    assert entry["channels"] == ["C1N", "C2N", "C3N"]


def test_apply_pending_adds_the_pending_routes_in_a_drained_window_and_records_them(env, monkeypatch):
    cfg, oc, fake, o = env
    s = state.SetupState()
    s.openclaw = o
    o.host_restart_pending = [{"op": "route", "path": "bindings", "channels": ["C1N"], "source": "setup", "at": "T"}]
    from ai_resources.setup.cockpits import openclaw as cockpit
    monkeypatch.setattr(cockpit, "config_path", lambda: cfg)
    monkeypatch.setattr(cockpit, "apply_patch", lambda patch, *, dry_run=False, replace_paths=None, allow_restart=False, reload_mode=None:
                        _apply_unchecked(oc, patch, dry_run, replace_paths))
    monkeypatch.setattr(section, "_drained_apply", lambda patch, rp, apply: (_apply_unchecked(oc, patch, False, rp)[0], True))
    assert section.apply_pending(s, assume_yes=True) == 0
    live = json.loads(cfg.read_text())
    assert [e["match"]["peer"]["id"] for e in live["bindings"] if e.get("agentId") == nr.AGENT_ID] == ["C1N"]
    assert [r["kind"] for r in o.notification_route_changes] == ["agent", "binding"] and o.host_restart_pending == []


def _apply_unchecked(oc, patch, dry_run, replace_paths):
    args = ["config", "patch", "--stdin"] + (["--dry-run"] if dry_run else [])
    for rp in replace_paths or []:
        args += ["--replace-path", rp]
    rc, out = oc(args, stdin=json.dumps(patch))
    return rc == 0, out


# --- teardown --------------------------------------------------------------------------------------------------------

def test_teardown_removes_only_the_kit_entries(env):
    cfg, oc, fake, o = env
    _offer(env)
    doc = json.loads(cfg.read_text())
    assert section._teardown_notification_route(o, _apply(oc), doc) is True
    live = json.loads(cfg.read_text())
    assert live["bindings"] == [OPS, TG, CATCH_ALL] and nr.AGENT_ID not in live["agents"]["entries"]
    assert live["agents"]["entries"]["main"] == DOC["agents"]["entries"]["main"]
    assert o.notification_route_changes == []


def test_teardown_spares_an_operator_edit_and_a_route_the_operator_added(env):
    cfg, oc, fake, o = env
    _offer(env)
    doc = json.loads(cfg.read_text())
    for e in doc["bindings"]:
        if e.get("agentId") == nr.AGENT_ID and e["match"]["peer"]["id"] == "C2N":
            e["comment"] = "I edited this"                                # no longer what the kit wrote
    doc["bindings"].insert(2, {"type": "route", "agentId": nr.AGENT_ID, "comment": "mine",
                               "match": {"channel": "slack", "peer": {"kind": "channel", "id": "C3N"}}})
    cfg.write_text(json.dumps(doc))
    assert section._teardown_notification_route(o, _apply(oc), json.loads(cfg.read_text())) is True
    live = json.loads(cfg.read_text())
    kept = [e for e in live["bindings"] if e.get("agentId") == nr.AGENT_ID]
    assert sorted(e["match"]["peer"]["id"] for e in kept) == ["C2N", "C3N"]
    assert nr.AGENT_ID in live["agents"]["entries"]                         # still routed to: the agent stays
    assert any("still has routes" in m for m in fake.messages("warn"))


def test_teardown_leaves_an_agent_the_operator_changed(env):
    cfg, oc, fake, o = env
    _offer(env)
    doc = json.loads(cfg.read_text())
    doc["agents"]["entries"][nr.AGENT_ID]["thinkingDefault"] = "high"
    cfg.write_text(json.dumps(doc))
    section._teardown_notification_route(o, _apply(oc), json.loads(cfg.read_text()))
    live = json.loads(cfg.read_text())
    assert nr.AGENT_ID in live["agents"]["entries"] and live["bindings"] == [OPS, TG, CATCH_ALL]
    assert any("was changed since" in m for m in fake.messages("warn"))


def test_a_hand_edited_record_cannot_make_teardown_delete_other_things(env):
    cfg, oc, fake, o = env
    o.notification_route_changes = [{"kind": "binding", "entry": OPS}, {"kind": "agent", "id": "main", "entry": {}}]
    before = cfg.read_bytes()
    assert section._teardown_notification_route(o, _apply(oc), json.loads(before)) is True
    assert cfg.read_bytes() == before


def test_teardown_does_nothing_when_nothing_was_recorded_and_the_state_omits_the_field():
    assert "notification_route_changes" not in state._to_dict(state.SetupState())["openclaw"]
    patch, rp, notes = nr.restore_plan([], DOC)
    assert (patch, rp, notes) == ({}, [], [])


def test_the_keys_are_hot_in_the_reload_table_of_this_openclaw():
    rs = rr.RuleSet("b", "installed", rr.BASELINE_RULES, "")
    assert set(rr.classify(["bindings", f"agents.entries.{nr.AGENT_ID}.model.primary"], ruleset=rs).values()) <= {rr.HOT, rr.NONE}
