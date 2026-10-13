"""Closure of Verifier finding F6: loss clamping and cap, undated DeepSeek evidence, the sidecar read time and
the refresh note. Scores are ILLUSTRATIVE FIXTURES, never evidence."""
from __future__ import annotations

import json
import os

import pytest
import yaml

from ai_resources import cli, models_cmd
from ai_resources.scoring import db as scores_db
from ai_resources.scoring import headroom as hr
from ai_resources.scoring import headroom_sources as hs
from ai_resources.scoring.compose import compose
from scoring_fakes import OPUS, SONNET, SOL, fixture_db, make_input, row, shipped_copy

SHIPPED = shipped_copy()


# --- loss: clamped at zero, and a cap that can only bind where the shape contract forces a loss -----------
def test_weighted_loss_is_never_negative_when_an_unverified_pick_beats_the_verified_anchor():
    rows = [row("implementer", SONNET, 6.0), row("implementer", SOL, 9.5)]
    comp = compose(make_input(fixture_db(rows), roles=("implementer",), enabled=("anthropic", "openai"),
                              allow_unverified=True, static_weights={"claude-subscription": 0.3}))
    assert comp.assignments[0].model == SOL, "fixture must make the unverified pick win for the test to mean anything"
    assert comp.weighted_loss == 0.0


def test_the_loss_cap_binds_when_the_shape_contract_forces_a_role_out_of_band():
    rows = [row("planner", SONNET, 9.5), row("planner", OPUS, 3.0),
            row("implementer", SONNET, 3.0), row("implementer", OPUS, 9.5)]
    comp = compose(make_input(fixture_db(rows), mode="uniform", roles=("planner", "implementer"),
                              enabled=("anthropic",)))
    assert comp.refused and "exceeds the cap" in comp.refusal


def test_the_cap_cannot_bind_in_mixed_mode_by_construction_and_the_policy_says_so():
    policy = SHIPPED["policy"]
    most = sum(policy["importance"][r] * policy["tolerance_classes"][policy["role_class"][r]]
               for r in policy["importance"])
    assert most < policy["loss_cap"]
    assert "mixed" in policy["notes"] and "cap" in policy["notes"] and "uniform" in policy["notes"]


# --- undated evidence is not given a date it does not have ---------------------------------------------
def test_deepseek_evidence_is_not_dated_2026_10_12_when_the_board_date_was_not_recorded():
    models = SHIPPED["models"]["deepseek/deepseek-v4-pro"]
    proxy = next(b for b in models["benchmarks"] if b["board"] == "vals-terminal-bench-4.0")
    assert proxy["as_of"] != "2026-10-12"
    assert "not recorded" in proxy["notes"]
    rows = [r for r in SHIPPED["rows"] if r["model"] == "deepseek/deepseek-v4-pro"]
    assert rows
    assert all(r["as_of"] == proxy["as_of"] for r in rows)
    assert not any("updated 2026-10-12" in r.get("basis", "") for r in rows)


# --- the sidecar read time is the file's own timestamp ----------------------------------------------------
def _sidecar(tmp_path, mtime):
    p = tmp_path / "rl.json"
    p.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": 4_000_000_000}}}))
    os.utime(p, (mtime, mtime))
    return p


def test_sidecar_read_at_is_the_file_mtime_not_now(tmp_path):
    p = _sidecar(tmp_path, 1_700_000_000)
    r = hs.read_claude_sidecar(p, now_epoch=1_700_000_100)
    assert r.read_at == 1_700_000_000


def test_an_old_sidecar_file_is_stale_and_the_static_weight_is_used(tmp_path):
    p = _sidecar(tmp_path, 1_700_000_000)
    now = 1_700_000_000 + 6 * 3600
    r = hs.read_claude_sidecar(p, now_epoch=now)
    ew = hr.effective_weight(0.7, r, now)
    assert ew.basis == "static" and ew.weight == 0.7


# --- refresh says plainly what it does not propose --------------------------------------------------------
@pytest.fixture
def override_path(tmp_path, monkeypatch):
    p = tmp_path / "xdg" / "ai-resources" / "model-scores.yaml"
    monkeypatch.setattr(scores_db, "default_override_path", lambda: p)
    return p


def test_refresh_states_that_prices_are_not_proposed(override_path, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(models_cmd, "_can_prompt", lambda args: False)
    assert cli.main(["models", "scores", "refresh"]) == 0
    out = capsys.readouterr().out
    assert "prices are not proposed" in out
    f = tmp_path / "p.yaml"
    f.write_text(yaml.safe_dump({"rows": [{
        "role": "tester", "model": "anthropic/claude-haiku-5.5", "route": "anthropic-direct", "effort": "default",
        "score": 5.0, "label": "VENDOR", "as_of": "2026-10-20", "source_ref": "operator", "reason": "x"}]}))
    assert cli.main(["models", "scores", "refresh", "--from", str(f)]) == 10
    assert "prices are not proposed" in capsys.readouterr().out
