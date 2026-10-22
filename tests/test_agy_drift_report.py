"""Drift report: an agy lane that runs a Claude model on the API (cost analysis 2026-10-22). Report only, no repoint."""
from __future__ import annotations

from ai_resources import agy_lane_gates
from ai_resources.setup.cockpits import openclaw_lanes as ol

DOC = {"agents": {"entries": {
    "kit-lane-agy-opus55": {"model": {"primary": "anthropic/claude-opus-5-5"}},
    "kit-lane-agy-gemini4argonhigh": {"model": "anthropic/claude-opus-5-5"},
    "kit-lane-agy-ok": {"model": {"primary": "agy-cli-lane/gemini-3.8-flash-low"}},
    "kit-lane-cli-opus55": {"model": {"primary": "anthropic/claude-opus-5-5"}},
    "main": {"model": {"primary": "anthropic/claude-sonnet-5-5"}}}}}


def test_an_agy_lane_on_an_anthropic_primary_is_reported_and_others_are_not():
    assert [a for a, _ in ol.agy_primary_drift(DOC)] == ["kit-lane-agy-gemini4argonhigh", "kit-lane-agy-opus55"]
    msgs = ol.agy_primary_drift_findings(DOC)
    assert len(msgs) == 2 and all("does not repoint" in m and "ADR-0007" in m for m in msgs)


def test_the_report_does_not_change_the_lane_gates():
    before = agy_lane_gates.all_proven()
    ol.agy_primary_drift_findings(DOC)
    assert agy_lane_gates.all_proven() == before


def test_the_report_never_modifies_the_config_it_reads():
    import copy
    doc = copy.deepcopy(DOC)
    ol.agy_primary_drift(doc)
    assert doc == DOC


def test_a_malformed_config_reports_nothing():
    assert ol.agy_primary_drift({}) == []
    assert ol.agy_primary_drift({"agents": {"entries": "x"}}) == []
    assert ol.agy_primary_drift({"agents": {"entries": {"kit-lane-agy-a": None}}}) == []


def test_the_wizard_warns_early_about_an_agy_lane_on_the_api(monkeypatch):
    from ai_resources.setup import state, ui
    from ai_resources.setup.cockpits import _openclaw_host as section
    seen = []
    monkeypatch.setattr(ui, "warn", seen.append)
    monkeypatch.setattr(ui, "info", lambda *_a, **_k: None)
    monkeypatch.setattr(ui, "detail", lambda *_a, **_k: None)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    o = state.OpenClawState()
    o.host_config = False                        # the fill step is skipped; the warnings still run only with host_config
    section._configure_config(o, DOC, __import__("pathlib").Path("/x"), [], dry_run=True,
                              apply_patch=lambda *a, **k: (True, "ok"))
    assert seen == []
    o.host_config = True
    monkeypatch.setattr(section, "_configure_config_fill", lambda *a, **k: False)
    section._configure_config(o, DOC, __import__("pathlib").Path("/x"), [], dry_run=True,
                              apply_patch=lambda *a, **k: (True, "ok"))
    assert any("kit-lane-agy-opus55" in m for m in seen)
