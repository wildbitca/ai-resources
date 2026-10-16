"""FU-5: a measured headroom separates otherwise equal lane candidates (ADR-0007 OQ-4); static weights otherwise."""
from __future__ import annotations

import json
import os

from ai_resources.scoring import headroom_sources, lanes as L
from ai_resources.setup import lanes_render

NOW = 1_800_000_000
GATES = {"antigravity_flag": True, "adr_0007_agy_accepted": True}
ACKS = {"anthropic": {"roles": ["code-reviewer"]}, "google": {"roles": ["code-reviewer"]}}
BY_ROLE = {"code-reviewer": {"model": "sonnet"}}
AGENTS = {
    "kit-lane-cli": {"id": "kit-lane-cli", "backend": "claude-cli", "model": "claude-sonnet-5-5"},
    "kit-lane-agy": {"id": "kit-lane-agy", "backend": "agy-cli-lane", "model": "claude-sonnet-5-5"},
}
CANDS = {"code-reviewer": ["openclaw-agent:kit-lane-cli", "openclaw-agent:kit-lane-agy"]}


def _sidecar(tmp_path, used, age=0):
    p = tmp_path / "rl.json"
    p.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": used, "resets_at": NOW + 3600}}}))
    os.utime(p, (NOW - age, NOW - age))
    return p


def _resolve(weights):
    return L.resolve_lanes(BY_ROLE, AGENTS, CANDS, None, weights=weights, gates=GATES, acks=ACKS)


def test_static_fallback_when_no_reading(tmp_path):
    deps = {"claude_sidecar_path": tmp_path / "absent.json", "kit_counters_path": tmp_path / "none.json"}
    assert headroom_sources.lane_weights(deps=deps, now_epoch=NOW) == {}
    # claude pool static 1.0 vs agy static 0.7: the declared first candidate wins on pressure
    assert _resolve({}).roles["code-reviewer"].agentId == "kit-lane-cli"


def test_measured_headroom_separates_equal_candidates(tmp_path):
    deps = {"claude_sidecar_path": _sidecar(tmp_path, 90), "kit_counters_path": tmp_path / "none.json"}
    w = headroom_sources.lane_weights(deps=deps, now_epoch=NOW)
    assert w == {"claude-subscription": 0.1}
    assert _resolve(w).roles["code-reviewer"].agentId == "kit-lane-agy"


def test_a_stale_reading_is_ignored(tmp_path):
    deps = {"claude_sidecar_path": _sidecar(tmp_path, 90, age=6 * 3600), "kit_counters_path": tmp_path / "n.json"}
    assert headroom_sources.lane_weights(deps=deps, now_epoch=NOW) == {}


def test_kit_counters_feed_non_claude_pools(tmp_path):
    c = tmp_path / "usage-counters.json"
    c.write_text(json.dumps({"agy-claude-gpt": {"used": 3, "limit": 4}}))
    os.utime(c, (NOW, NOW))
    deps = {"claude_sidecar_path": tmp_path / "a.json", "kit_counters_path": c}
    assert headroom_sources.lane_weights(deps=deps, now_epoch=NOW) == {"agy-claude-gpt": 0.175}  # static 0.7 x (1 - 0.75)


def test_unreadable_files_never_raise(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    deps = {"claude_sidecar_path": bad, "kit_counters_path": bad}
    assert headroom_sources.lane_weights(deps=deps, now_epoch=NOW) == {}


def test_deterministic(tmp_path):
    deps = {"claude_sidecar_path": _sidecar(tmp_path, 37), "kit_counters_path": tmp_path / "n.json"}
    a = headroom_sources.lane_weights(deps=deps, now_epoch=NOW)
    b = headroom_sources.lane_weights(deps=deps, now_epoch=NOW)
    assert a == b
    assert _resolve(a).digest == _resolve(b).digest
    assert _resolve(a).digest != _resolve({}).digest


def _spy_resolve(monkeypatch):
    """Record the `weights` every resolve_lanes call receives while delegating to the real resolver."""
    seen: list[dict] = []
    real = lanes_render.scoring_lanes.resolve_lanes

    def spy(*a, **kw):
        seen.append(dict(kw.get("weights") or {}))
        return real(*a, **kw)

    monkeypatch.setattr(lanes_render.scoring_lanes, "resolve_lanes", spy)
    return seen


def _lanes_state():
    return {"agents": {"kit-lane-cli": {"backend": "claude-cli", "model": "anthropic/claude-sonnet-5-5",
                                        "pool": "claude-subscription"}},
            "roles": {"implementer": {"candidates": ["claude-code", "openclaw-agent:kit-lane-cli"]}},
            "second": {}, "pool_caps": {}}


def test_snapshot_in_lanes_state_reaches_the_resolver(monkeypatch):
    # render_doc passes no agy/codex gates, so only the claude pool is admissible there and a weight cannot
    # flip a pick; the wiring is proven by what the resolver receives (the flip itself is covered above).
    ack = {"anthropic": {"roles": ["implementer"]}}
    by_role = {"implementer": {"model": "sonnet"}}
    seen = _spy_resolve(monkeypatch)
    lanes_render.render_doc(_lanes_state(), by_role, "single-model", ack)
    assert seen and all(w == {} for w in seen)
    seen.clear()
    with_w = lanes_render.render_doc({**_lanes_state(), "headroom_weights": {"claude-subscription": 0.2}},
                                     by_role, "single-model", ack)
    assert seen and all(w == {"claude-subscription": 0.2} for w in seen)
    assert "error" not in with_w["resolved"]


def test_propose_stores_the_headroom_snapshot_sorted():
    from ai_resources.setup import profiles
    doc = profiles.load_profile("subscription-claude-55")
    out = lanes_render.propose(doc, doc["by_role"], "single-model",
                               headroom={"claude-subscription": 1, "agy-claude-gpt": 0.2})
    assert list(out["headroom_weights"].items()) == [("agy-claude-gpt", 0.2), ("claude-subscription", 1.0)]
    assert "headroom_weights" not in lanes_render.propose(doc, doc["by_role"], "single-model")
