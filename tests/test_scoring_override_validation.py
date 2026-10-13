"""Closure of Verifier finding F4: override rows and refresh proposals pass the same evidence rules as shipped
rows. Scores are ILLUSTRATIVE FIXTURES, never evidence."""
from __future__ import annotations

import json

import pytest
import yaml

from ai_resources import cli, models_cmd
from ai_resources.scoring import db as scores_db
from ai_resources.setup import ui

GOOD = {"role": "tester", "model": "anthropic/claude-haiku-5.5", "route": "anthropic-direct", "effort": "default",
        "score": 5.0, "label": "VENDOR", "as_of": "2026-10-20", "source_ref": "operator", "reason": "re-measured"}

BAD_ROWS = {
    "score above range": dict(GOOD, score=99),
    "score below range": dict(GOOD, score=-1),
    "score not a number": dict(GOOD, score="high"),
    "MEASURED-HERE without evidence fields": dict(GOOD, score=7.0, label="MEASURED-HERE"),
    "INFERRED without basis": dict(GOOD, label="INFERRED"),
    "unknown role": dict(GOOD, role="no-such-role"),
    "unknown model": dict(GOOD, model="anthropic/claude-nope-9"),
    "unknown route": dict(GOOD, route="no-such-route"),
    "unknown label": dict(GOOD, label="GUESSED"),
    "bad date": dict(GOOD, as_of="yesterday"),
}


@pytest.fixture
def override_path(tmp_path, monkeypatch):
    p = tmp_path / "xdg" / "ai-resources" / "model-scores.yaml"
    monkeypatch.setattr(scores_db, "default_override_path", lambda: p)
    return p


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"rows": list(rows)}))


@pytest.mark.parametrize("name", sorted(BAD_ROWS))
def test_merge_rejects_an_override_row_that_breaks_the_evidence_rules(name):
    shipped = scores_db.load_shipped()
    merged, problems = scores_db.merge(shipped, {"rows": [BAD_ROWS[name]]})
    assert problems, name
    assert merged["rows"] == shipped["rows"], "an invalid override must not enter the merged rows"


def test_merge_still_accepts_a_valid_override_row():
    shipped = scores_db.load_shipped()
    merged, problems = scores_db.merge(shipped, {"rows": [GOOD]})
    assert problems == []
    assert any(r.get("override") for r in merged["rows"])


def test_a_measured_here_override_with_its_evidence_fields_is_accepted():
    row = dict(GOOD, score=7.0, label="MEASURED-HERE", run_id="r1", n=30, harness_version="1", corpus_hash="abc",
               low_power=False)
    merged, problems = scores_db.merge(scores_db.load_shipped(), {"rows": [row]})
    assert problems == [], problems


def test_models_scores_check_reports_the_invalid_override(override_path, capsys):
    _write(override_path, [dict(BAD_ROWS["MEASURED-HERE without evidence fields"], score=99)])
    assert cli.main(["models", "scores", "check", "--as-of", "2026-10-13", "--json"]) == 1
    findings = json.loads(capsys.readouterr().out)
    text = json.dumps([f for f in findings if f["kind"] in ("override-problem", "invalid")])
    assert "score" in text and "MEASURED-HERE" in text


def test_a_refresh_proposal_row_is_validated_before_it_is_written(override_path, tmp_path, monkeypatch, capsys):
    bad = tmp_path / "proposals.yaml"
    bad.write_text(yaml.safe_dump({"rows": [dict(GOOD, score=99)]}))
    monkeypatch.setattr(models_cmd, "_can_prompt", lambda args: True)
    monkeypatch.setattr(ui, "select", lambda message, choices, default=None, **k: "approve")
    rc = cli.main(["models", "scores", "refresh", "--from", str(bad)])
    out = capsys.readouterr()
    assert rc != 0 or "invalid" in (out.out + out.err).lower()
    assert not override_path.exists()
    assert "invalid" in (out.out + out.err).lower()
