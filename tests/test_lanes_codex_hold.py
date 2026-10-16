"""FU-2: the Codex lane backend is registered but HELD like agy; read-only roles; degrades without codex."""
from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest

from ai_resources import lanes_cmd
from ai_resources.scoring import lanes as L
from ai_resources.setup import lanes_render
from ai_resources.setup.cockpits import openclaw_lanes

IDS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
BLOCK = {"roles": {"explore": ["claude-cli:sonnet", "codex:gpt-5.5-codex"]},
         "second": {"code-reviewer": ["codex:gpt-5.5-codex"]}}
ADR = Path(__file__).resolve().parents[1] / "docs" / "decisions" / "0007-subscription-lanes.md"


def test_flag_is_off_and_backend_is_not_writable():
    assert lanes_render.CODEX_LANE_ADR_ACCEPTED is False
    assert "codex-cli" not in openclaw_lanes.WRITABLE_BACKENDS
    assert "codex-cli" in L.KNOWN_BACKENDS


def test_adr_records_the_codex_follow_up_as_held():
    text = ADR.read_text()
    assert "Codex lane" in text and "CODEX_LANE_ADR_ACCEPTED" in text


def test_codex_candidates_are_held_with_a_reason_and_never_become_agents():
    frag = lanes_render.state_from_block(BLOCK, claude_ids=IDS)
    assert all(a["backend"] == "claude-cli" for a in frag["agents"].values())
    assert frag["roles"]["explore"]["candidates"] == ["openclaw-agent:kit-lane-cli-sonnet55"]
    assert "code-reviewer" not in frag["second"]
    assert {h["reason"] for h in frag["held"]} == {"held: Codex lane not accepted"}
    assert len(frag["held"]) == 2
    assert "held: Codex lane not accepted" in lanes_render.explain_lanes(frag)["explore"]


def test_a_codex_candidate_for_a_writer_role_is_a_block_problem():
    probs = lanes_render.validate_block({"roles": {"implementer": ["codex:gpt-5.5-codex"]}})
    assert any("read-only" in p and "implementer" in p for p in probs)
    assert lanes_render.validate_block({"roles": {"explore": ["codex:gpt-5.5-codex"]}}) == []


def test_a_hand_edited_state_codex_agent_never_reaches_lanes_yaml_or_the_writer():
    lanes = {"agents": {"kit-lane-codex-gpt55codex": {"backend": "codex-cli", "model": "gpt-5.5-codex",
                                                     "pool": "codex-subscription"}},
             "roles": {"explore": {"candidates": ["openclaw-agent:kit-lane-codex-gpt55codex", "claude-code"]}},
             "second": {}, "pool_caps": {}}
    doc = lanes_render.render_doc(lanes, {"explore": {"model": "sonnet"}}, "single-model")
    assert doc["agents"] == {}
    assert doc["roles"]["explore"]["candidates"] == ["claude-code"]
    assert openclaw_lanes.held_agents(lanes) == {
        "kit-lane-codex-gpt55codex": "held: backend codex-cli is not writable in this version"}
    assert openclaw_lanes.kit_agent_ids(lanes) == []
    assert openclaw_lanes._catalog_entry(lanes["agents"]["kit-lane-codex-gpt55codex"]) is None
    assert "codex-cli" in openclaw_lanes.CATALOG_SHAPES      # the proposed shape is recorded, not written


def _agents():
    return {"kit-lane-codex-gpt": L.LaneAgent("kit-lane-codex-gpt", "codex-cli", "gpt-5.5-codex")}


def test_pool_and_admission_gates():
    a = _agents()["kit-lane-codex-gpt"]
    assert L.pool_of_agent(a) == "codex-subscription"
    kw = dict(by_role={"explore": {"model": "gpt-5.5-codex"}, "implementer": {"model": "gpt-5.5-codex"}},
              agents=_agents(), mode="single-model")
    cand = {"explore": ["openclaw-agent:kit-lane-codex-gpt"]}
    table = L.resolve_lanes(candidates=cand, **kw)                     # no gate, no ack: held
    assert table.roles["explore"].ref == "claude-code"
    assert "not accepted" in "; ".join(w.text for w in table.why if w.role == "explore") or \
        "codex" in "; ".join(w.text for w in table.why if w.role == "explore").lower()
    gates = {"codex_flag": True, "adr_0007_codex_accepted": True}
    acks = {"openai": {"roles": ["explore", "implementer"]}}
    ok = L.resolve_lanes(candidates=cand, gates=gates, acks=acks, **kw)
    assert ok.roles["explore"].ref == "openclaw-agent:kit-lane-codex-gpt"
    bad = L.resolve_lanes(candidates={"implementer": ["openclaw-agent:kit-lane-codex-gpt"]},
                          gates=gates, acks=acks, **kw)                # a writer role is never admitted
    assert bad.roles["implementer"].ref == "claude-code"
    assert "read-only" in "; ".join(w.text for w in bad.why if w.role == "implementer")


def test_lanes_prepare_refuses_a_codex_agent_even_with_a_recomputed_digest():
    doc = {"agents": {"kit-lane-codex-gpt": {"backend": "codex-cli", "model": "gpt-5.5-codex",
                                             "pool": "codex-subscription"}}}
    with pytest.raises(lanes_cmd.Refused):
        lanes_cmd._admit_lane(doc, "openclaw-agent:kit-lane-codex-gpt")


def test_status_degrades_to_not_installed_without_a_binary(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _n, *a, **k: None)
    st = lanes_render.codex_status()
    assert st == {"installed": False, "path": "", "held": True, "summary": "codex: not installed (lane held)"}


def test_status_reports_a_binary_when_present(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _n, *a, **k: "/usr/bin/codex")
    st = lanes_render.codex_status()
    assert st["installed"] is True and st["held"] is True
