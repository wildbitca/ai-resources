"""Composition output: customizations over a profile through the existing merge, same-vendor fallbacks only."""
from __future__ import annotations

from ai_resources.scoring import profile_out
from ai_resources.scoring.compose import compose
from ai_resources.setup import profiles
from scoring_fakes import HAIKU, OPUS, SONNET, SOL, fixture_db, make_input, row

BASELINE_NAME = "balanced-claude-55"


def build(**kw):
    rows = []
    for role in ("planner", "implementer", "tester"):
        rows += [row(role, SONNET, 6.4), row(role, OPUS, 6.0), row(role, HAIKU, 5.0), row(role, SOL, 6.3)]
    kw.setdefault("enabled", ("anthropic", "openai"))
    kw.setdefault("allow_unverified", True)
    return compose(make_input(fixture_db(rows), roles=("planner", "implementer", "tester"), **kw))


def test_customizations_merge_into_the_profile_and_reach_executors():
    comp = build(static_weights={"claude-subscription": 0.3, "openai-api": 1.0})
    custom = profile_out.to_customizations(comp)
    assert set(custom) == {"planner", "implementer", "tester"}
    base = profiles.load_profile(BASELINE_NAME)
    merged = profiles.merge_customizations(base, custom)
    ex = profiles.to_executors(merged, "litellm")
    for role, entry in custom.items():
        assert ex["by_role"][role]["model"] == entry["model"]
    # roles the composition did not touch keep the profile's assignment
    assert ex["by_role"]["explore"] == base["by_role"]["explore"]


def test_no_backend_or_agentid_and_fallbacks_stay_in_the_vendor():
    custom = profile_out.to_customizations(build())
    for role, entry in custom.items():
        assert set(entry) <= {"model", "fallbacks"}
        vendor = entry["model"].split("/", 1)[0]
        assert all(f.startswith(vendor + "/") for f in entry.get("fallbacks", []))


def test_a_kept_default_or_a_refusal_writes_no_override():
    refused = compose(make_input(fixture_db([]), roles=("planner",), enabled=()))
    assert profile_out.to_customizations(refused) == {}


def test_provenance_records_digest_labels_and_headroom_per_pool():
    comp = build()
    prov = profile_out.provenance(comp)
    assert prov["input_digest"] == comp.input_digest and prov["dataset_version"] == "2026-10-13"
    assert prov["allow_unverified"] is True and set(prov["labels"]) == {"planner", "implementer", "tester"}
    assert all(v == "INFERRED" for v in prov["labels"].values())
    assert prov["headroom"] and all(h["basis"] == "static" for h in prov["headroom"].values())


# -- lanes pass output (ADR-0007) ----------------------------------------------------------------------------

def test_customizations_are_byte_identical_with_and_without_a_lanes_table():
    import json

    from ai_resources.scoring import lanes

    comp = build(static_weights={"claude-subscription": 0.3, "openai-api": 1.0})
    before = json.dumps(profile_out.to_customizations(comp), sort_keys=True)
    by_role = {"implementer": {"model": "sonnet"}}
    agent = lanes.LaneAgent("kit-lane-cli-sonnet55", "claude-cli", "anthropic/claude-sonnet-5-5")
    table = lanes.resolve_lanes(by_role, {agent.id: agent}, {"implementer": [f"openclaw-agent:{agent.id}"]},
                                acks={"anthropic": {"roles": ["implementer"]}})
    out = profile_out.to_lanes(table)
    assert json.dumps(profile_out.to_customizations(comp), sort_keys=True) == before
    assert set(out) == {"agents", "roles", "second"}
    assert out["roles"]["implementer"] == {"lane": f"openclaw-agent:{agent.id}"}
    assert out["agents"][agent.id]["pool"] == "claude-subscription"
    assert by_role == {"implementer": {"model": "sonnet"}}          # by_role never gains lane keys
    assert "lane" not in json.dumps(by_role)
