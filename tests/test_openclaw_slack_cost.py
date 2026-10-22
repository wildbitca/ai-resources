"""The narrow Slack cost writer (operator decision Q1, ADR-0013): two keys, fill-only, confirmed, reversible."""
from __future__ import annotations

import json

import pytest

from ai_resources import openclaw_host as host
from ai_resources import openclaw_slack_cost as sc
from ai_resources.setup import state, ui
from ai_resources.setup.cockpits import _openclaw_host as section
from test_openclaw_host_wizard import FakeOpenclaw, ScriptedUi  # noqa: F401

DOC = {"channels": {"slack": {"enabled": True, "channels": {"C1": {"enabled": True}, "C2": {"enabled": True},
                                                            "C3": {"allowBots": True}}}}}


# --- the guard -------------------------------------------------------------------------------------------------

def test_the_two_keys_pass_the_guard():
    sc.assert_slack_cost_safe(sc.build_patch([sc.THREAD_PATH, sc.allow_bots_path("C1")]))


@pytest.mark.parametrize("patch", [
    {"channels": {"slack": {"allowFrom": ["U1"]}}},
    {"channels": {"slack": {"channels": {"C1": {"enabled": False}}}}},
    {"channels": {"slack": {"channels": {"C1": {"allowBots": True}}}}},
    {"channels": {"slack": {"implicitMentions": {"threadParticipation": True}}}},
    {"channels": {"slack": {"botToken": "x"}}},
    {"channels": {"telegram": {"streaming": {"mode": "progress"}}}},
    {"channels": {"slack": {"channels": {"../x": {"allowBots": "mentions"}}}}},
    {"tools": {"deny": ["x"]}},
])
def test_anything_else_is_refused(patch):
    with pytest.raises(ValueError):
        sc.assert_slack_cost_safe(patch)


def test_a_replace_path_is_refused():
    with pytest.raises(ValueError):
        sc.assert_slack_cost_safe(sc.build_patch([sc.THREAD_PATH]), ["channels.slack"])


def test_the_host_patch_guard_is_unchanged_and_still_refuses_slack():
    with pytest.raises(ValueError):
        host.assert_channels_safe({"channels": {"slack": {"implicitMentions": {"threadParticipation": False}}}})
    built = host.build_host_patch(host.load_host_profile(), {}, {"DOMAIN": "a.example.org", "POD_CIDR": "10.0.0.0/24"})
    assert "slack" not in json.dumps(built["patch"].get("channels", {}))


# --- fill-only proposals -----------------------------------------------------------------------------------------

def test_thread_participation_is_proposed_only_when_the_operator_has_not_set_it():
    assert sc.thread_proposal(DOC) == sc.THREAD_PATH
    set_doc = {"channels": {"slack": {"implicitMentions": {"threadParticipation": True}}}}
    assert sc.thread_proposal(set_doc) is None


def test_a_channel_with_its_own_allow_bots_is_not_a_candidate():
    assert sc.allow_bots_candidates(DOC) == ["C1", "C2"]


# --- through the wizard ------------------------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps(json.loads(json.dumps(DOC))))
    oc = FakeOpenclaw(cfg)
    fake = ScriptedUi()
    fake.install(monkeypatch)
    fake.picked = ["C1"]
    monkeypatch.setattr(ui, "checkbox", lambda msg, choices, default=None, **k: (fake.asked.append(msg), fake.picked)[1])
    fake.answers = {"Stop the Slack bot answering": True}
    o = state.OpenClawState()
    o.host_config = True
    return cfg, oc, fake, o


def _apply(oc):
    def apply_patch(patch, *, dry_run=False, replace_paths=None):
        args = ["config", "patch", "--stdin"] + (["--dry-run"] if dry_run else [])
        for rp in replace_paths or []:
            args += ["--replace-path", rp]
        rc, out = oc(args, stdin=json.dumps(patch))
        return rc == 0, out
    return apply_patch


def test_confirming_sets_both_keys_and_records_them(env):
    cfg, oc, fake, o = env
    doc = json.loads(cfg.read_text())
    written: list = []
    assert section._offer_slack_cost(o, doc, cfg, written, dry_run=False, apply_patch=_apply(oc)) is True
    live = json.loads(cfg.read_text())["channels"]["slack"]
    assert live["implicitMentions"]["threadParticipation"] is False
    assert live["channels"]["C1"]["allowBots"] == "mentions"
    assert "allowBots" not in live["channels"]["C2"] and live["channels"]["C3"]["allowBots"] is True
    assert len(o.slack_cost_changes) == 2 and written == [cfg]


def test_declining_the_thread_question_writes_no_thread_key(env):
    cfg, oc, fake, o = env
    fake.answers = {"Stop the Slack bot answering": False}
    fake.picked = []
    section._offer_slack_cost(o, json.loads(cfg.read_text()), cfg, [], dry_run=False, apply_patch=_apply(oc))
    assert json.loads(cfg.read_bytes()) == DOC and o.slack_cost_changes == []


def test_a_rerun_after_applying_asks_only_about_channels_still_without_allow_bots(env):
    cfg, oc, fake, o = env
    section._offer_slack_cost(o, json.loads(cfg.read_text()), cfg, [], dry_run=False, apply_patch=_apply(oc))
    fake.asked.clear()
    fake.picked = []
    section._offer_slack_cost(o, json.loads(cfg.read_text()), cfg, [], dry_run=False, apply_patch=_apply(oc))
    assert not [q for q in fake.asked if q.startswith("Stop the Slack bot")]
    assert len(o.slack_cost_changes) == 2


def test_non_interactive_writes_nothing_and_prints_the_commands(env):
    cfg, oc, fake, o = env
    fake.non_interactive = True
    before = cfg.read_bytes()
    assert section._offer_slack_cost(o, json.loads(before), cfg, [], dry_run=False, apply_patch=_apply(oc)) is False
    assert cfg.read_bytes() == before and not fake.asked
    assert any("openclaw config set channels.slack.implicitMentions.threadParticipation false" in m
               for m in fake.messages("detail"))


def test_a_dry_run_writes_nothing(env):
    cfg, oc, fake, o = env
    before = cfg.read_bytes()
    section._offer_slack_cost(o, json.loads(before), cfg, [], dry_run=True, apply_patch=_apply(oc))
    assert cfg.read_bytes() == before and not oc.real_patches()


def test_a_rejected_dry_run_applies_nothing(env):
    cfg, oc, fake, o = env
    oc.dry_run_ok = False
    before = cfg.read_bytes()
    section._offer_slack_cost(o, json.loads(before), cfg, [], dry_run=False, apply_patch=_apply(oc))
    assert cfg.read_bytes() == before and o.slack_cost_changes == []


def test_slack_disabled_or_absent_does_nothing(env):
    cfg, oc, fake, o = env
    assert section._offer_slack_cost(o, {}, cfg, [], dry_run=False, apply_patch=_apply(oc)) is False
    off = {"channels": {"slack": {"enabled": False, "channels": {"C1": {}}}}}
    assert section._offer_slack_cost(o, off, cfg, [], dry_run=False, apply_patch=_apply(oc)) is False
    assert not fake.asked


def test_every_patch_sent_passes_the_guard(env):
    cfg, oc, fake, o = env
    section._offer_slack_cost(o, json.loads(cfg.read_text()), cfg, [], dry_run=False, apply_patch=_apply(oc))
    for _args, patch in oc.calls:
        if patch:
            sc.assert_slack_cost_safe(patch)


def test_teardown_deletes_what_the_kit_set_and_spares_a_hand_edit(env):
    cfg, oc, fake, o = env
    fake.picked = ["C1", "C2"]
    section._offer_slack_cost(o, json.loads(cfg.read_text()), cfg, [], dry_run=False, apply_patch=_apply(oc))
    doc = json.loads(cfg.read_text())
    doc["channels"]["slack"]["channels"]["C2"]["allowBots"] = True          # the operator changed C2 afterwards
    cfg.write_text(json.dumps(doc))
    assert section._teardown_slack_cost(o, _apply(oc), json.loads(cfg.read_text())) is True
    live = json.loads(cfg.read_text())["channels"]["slack"]
    assert "implicitMentions" not in live and "allowBots" not in live["channels"]["C1"]
    assert live["channels"]["C2"]["allowBots"] is True and live["channels"]["C3"]["allowBots"] is True
    assert o.slack_cost_changes == []


def test_a_hand_edited_state_record_outside_the_two_keys_is_ignored(env):
    cfg, oc, fake, o = env
    changes = [{"path": ["channels", "slack", "allowFrom"], "previous": None, "had": False, "action": "filled",
                "delete": ["channels"]}]
    assert sc.restore_patch(changes, {"channels": {"slack": {"allowFrom": ["U"]}}}) == {}


def test_the_keys_are_hot_in_the_reload_table():
    from ai_resources import openclaw_reload_rules as rr
    rs = rr.RuleSet("b", "installed", rr.BASELINE_RULES, "")
    paths = [".".join(sc.THREAD_PATH), ".".join(sc.allow_bots_path("C1"))]
    assert set(rr.classify(paths, ruleset=rs).values()) == {rr.HOT}
