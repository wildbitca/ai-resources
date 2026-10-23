"""The confirmed migration of agy lanes that run on the Anthropic API (ADR-0015 D2b, operator answer 3)."""
from __future__ import annotations

import copy

import pytest

from ai_resources import agy_lane_gates
from ai_resources.setup import lanes_render
from ai_resources.setup.cockpits import openclaw_lanes as ol

STUB = {"models": {"providers": {"agy-cli-lane": {"models": [{"id": "claude-opus-5-5"},
                                                             {"id": "gemini-3.8-flash-low"}]}}}}


def doc():
    d = copy.deepcopy(STUB)
    d["agents"] = {"entries": {
        "kit-lane-agy-opus55": {"model": {"primary": "anthropic/claude-opus-5-5"}},
        "kit-lane-agy-sonnet": {"model": {"primary": "anthropic/claude-sonnet-5-5"}},
        "kit-lane-agy-ok": {"model": {"primary": "agy-cli-lane/gemini-3.8-flash-low"}},
        "kit-lane-cli-x": {"model": {"primary": "anthropic/claude-opus-5-5"}},
        "main": {"subagents": {"allowAgents": ["kit-lane-agy-opus55", "kit-lane-agy-sonnet", "x"]},
                 "model": {"primary": "anthropic/claude-opus-5-5"}}}}
    return d


class Fake:
    def __init__(self, ok=True, restart=()):
        self.patches, self.deleted, self.asked, self.ok, self.restart = [], [], [], ok, list(restart)

    def apply(self, patch, **k):
        self.patches.append(copy.deepcopy(patch))
        return self.ok, "rejected" if not self.ok else "ok"

    def delete(self, aid):
        self.deleted.append(aid)
        return 0, ""


def go(fake, *, acked, answers=True, **kw):
    def confirm(msg):
        fake.asked.append(msg)
        return answers if isinstance(answers, bool) else answers.pop(0)
    out: list[str] = []
    kw.setdefault("interactive", True)
    applied = ol.migrate_agy_drift(doc(), google_acknowledged=acked, apply_patch=fake.apply,
                                   classify=lambda p: fake.restart, confirm=confirm, delete_agent=fake.delete,
                                   info=out.append, warn=out.append, error=out.append, **kw)
    return applied, out


def test_proposals_repoint_when_acknowledged_and_the_catalog_has_the_model_else_remove():
    got = {p["id"]: p for p in ol.agy_drift_proposals(doc(), google_acknowledged=True)}
    assert got["kit-lane-agy-opus55"]["action"] == "repoint" and got["kit-lane-agy-opus55"]["to"] == "agy-cli-lane/claude-opus-5-5"
    assert got["kit-lane-agy-sonnet"]["action"] == "remove" and "no claude-sonnet-5-5" in got["kit-lane-agy-sonnet"]["why"]
    assert "kit-lane-agy-ok" not in got and "kit-lane-cli-x" not in got and "main" not in got
    nack = {p["id"]: p for p in ol.agy_drift_proposals(doc(), google_acknowledged=False)}
    assert {p["action"] for p in nack.values()} == {"remove"} and "acknowledgement" in nack["kit-lane-agy-opus55"]["why"]


def test_a_confirmed_repoint_patches_only_the_primary_of_that_agent():
    fake = Fake()
    applied, _ = go(fake, acked=True, answers=[True, False])         # opus55 yes; sonnet: the first confirm declines
    assert [p["id"] for p in applied] == ["kit-lane-agy-opus55"]
    assert fake.patches == [{"agents": {"entries": {"kit-lane-agy-opus55": {"model": {"primary":
                                                                                    "agy-cli-lane/claude-opus-5-5"}}}}}]
    assert fake.deleted == []


def test_nothing_is_applied_without_a_confirm_and_never_unattended():
    fake = Fake()
    applied, out = go(fake, acked=True, answers=False)
    assert applied == [] and fake.patches == [] and fake.deleted == []
    fake2 = Fake()
    applied2, out2 = go(fake2, acked=True, interactive=False)
    assert applied2 == [] and fake2.asked == [] and fake2.patches == [] and any("unattended" in m for m in out2)
    fake3 = Fake()
    applied3, out3 = go(fake3, acked=True, dry_run=True)
    assert applied3 == [] and fake3.asked == [] and any("dry run" in m for m in out3)


def test_a_removal_asks_twice_cleans_allow_agents_then_deletes():
    fake = Fake()
    applied, _ = go(fake, acked=False, answers=[True, True, False])   # opus55: apply yes, delete yes; sonnet declined
    assert [p["id"] for p in applied] == ["kit-lane-agy-opus55"]
    assert fake.deleted == ["kit-lane-agy-opus55"]
    assert fake.patches[0]["agents"]["entries"]["main"]["subagents"]["allowAgents"] == ["kit-lane-agy-sonnet", "x"]


def test_a_declined_second_confirm_deletes_nothing():
    fake = Fake()
    applied, _ = go(fake, acked=False, answers=[True, False, False])
    assert applied == [] and fake.deleted == [] and fake.patches == []


def test_a_restart_class_key_is_not_applied():
    fake = Fake(restart=["agents.entries.kit-lane-agy-opus55.model"])
    applied, out = go(fake, acked=True, answers=True)
    assert [p["id"] for p in applied] == ["kit-lane-agy-sonnet"]               # only the removal needs no restart class
    assert not any("primary" in str(p) for p in fake.patches)
    assert any("NOT applied" in m for m in out)


def test_a_rejected_patch_is_reported_and_not_counted():
    fake = Fake(ok=False)
    applied, out = go(fake, acked=True, answers=True)
    assert applied == [] and any("rejected" in m for m in out)


def test_only_kit_lane_agy_ids_are_ever_touched():
    fake = Fake()
    go(fake, acked=False, answers=True)
    assert all(a.startswith("kit-lane-agy-") for a in fake.deleted)


def test_the_gates_and_the_acknowledgement_text_are_untouched():
    before = (agy_lane_gates.all_proven(), lanes_render.ACK_TEXT["google"])
    go(Fake(), acked=True)
    assert (agy_lane_gates.all_proven(), lanes_render.ACK_TEXT["google"]) == before
    assert not agy_lane_gates.all_proven()


def test_a_new_agy_lane_is_still_held_while_the_gates_are_closed():
    lanes = {"agents": {"kit-lane-agy-new": {"backend": agy_lane_gates.AGY_LANE_BACKEND,
                                             "model": "agy-cli-lane/gemini-3.8-flash-low"}}}
    held = ol.held_agents(lanes, gates={}, acks={})
    assert "kit-lane-agy-new" in held
    assert ol.kit_agent_ids(lanes, gates={}, acks={}) == []
    for aid in ol.kit_agent_ids(lanes, gates={}, acks={}):
        assert not lanes["agents"][aid]["model"].startswith(("anthropic/", "claude-"))
