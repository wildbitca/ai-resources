"""Phase 3 gates G0..G7 are enforced data, not prose (ADR-0007 section 11)."""
from __future__ import annotations

import dataclasses

from ai_resources import agy_lane_gates as g, model_pins


def test_every_gate_is_declared_and_none_of_the_probe_stages_is_proven():
    assert [x.id for x in g.GATES] == ["G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7"]
    for gid in ("G0", "G1", "G2", "G3", "G4", "G5"):
        assert g.EVIDENCE[gid].status == g.NOT_PROVEN, gid
    assert {x.verification for x in g.GATES} >= {"V5", "V6", "V7", "V9", "V12"}


def test_the_lane_is_closed_while_any_gate_is_not_proven_and_the_message_names_them():
    assert not g.all_proven()
    msg = g.refusal()
    assert msg and msg.startswith("held:")
    for gid in ("G1", "G2", "G3", "G4", "G5"):
        assert gid in msg


def test_a_gate_is_proven_only_with_evidence():
    ev = {x.id: dataclasses.replace(x, status=g.PROVEN, evidence="run-dir+seal") for x in g.GATES}
    assert g.all_proven(ev) and g.refusal(ev) is None
    ev["G4"] = dataclasses.replace(ev["G4"], evidence="")
    assert [x.id for x in g.unproven(ev)] == ["G4"]
    del ev["G2"]
    assert [x.id for x in g.unproven(ev)] == ["G2", "G4"]            # a missing gate fails closed


def test_status_table_shape():
    table = g.status_table()
    assert set(table) == {f"G{i}" for i in range(8)}
    assert table["G5"] == {"status": "not proven", "evidence": ""}


def test_lane_slugs_are_independent_of_the_engine_list_and_marked_unconfirmed():
    assert model_pins.AGY_LANE_MODELS_CONFIRMED is False
    assert set(model_pins.AGY_STATIC) <= set(model_pins.AGY_LANE_MODELS)
    assert model_pins.AGY_STATIC == (
        "gemini-3.8-flash-low", "gemini-3.8-flash-high", "gemini-3.1-pro-low",
        "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium")
