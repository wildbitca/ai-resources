"""Closure of Verifier finding F6: the policy notes, undated DeepSeek evidence and the refresh note of the scores data."""
from __future__ import annotations

import pytest
import yaml

from ai_resources import cli, models_cmd
from ai_resources.scoring import db as scores_db
from scoring_fakes import shipped_copy

SHIPPED = shipped_copy()


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
