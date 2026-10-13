"""`ai-resources models scores show|check|diff|refresh`: read-only reports, proposals, no auto-approval."""
from __future__ import annotations

import copy
import contextlib
import json
import stat

import pytest
import yaml

from ai_resources import cli, models, models_cmd
from ai_resources.scoring import db as scores_db
from ai_resources.setup import ui


def run(*argv):
    return cli.main(["models", "scores", *argv])


@pytest.fixture
def override_path(tmp_path, monkeypatch):
    p = tmp_path / "xdg" / "ai-resources" / "model-scores.yaml"
    monkeypatch.setattr(scores_db, "default_override_path", lambda: p)
    return p


def write_override(path, rows=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"rows": list(rows)}))


ROW = {"role": "tester", "model": "anthropic/claude-haiku-5.5", "route": "anthropic-direct", "effort": "default",
       "score": 5.0, "label": "VENDOR", "as_of": "2026-10-20", "source_ref": "operator", "reason": "re-measured"}


_REAL_LOAD_SHIPPED = scores_db.load_shipped


def clean_shipped():
    """The shipped document with everything `check` would list removed (a fixture; not evidence)."""
    doc = copy.deepcopy(_REAL_LOAD_SHIPPED())
    doc["aliases"] = []
    probe = scores_db.ScoresDB(doc)
    for mid, m in doc["models"].items():
        for p in m.get("prices") or []:
            p.pop("verify", None)
        m["route_ids"] = {r: i for r, i in (m.get("route_ids") or {}).items() if probe.routable(mid, r)}
    kept = []
    for r in doc["rows"]:
        routes = [x for x in (r["route"] if isinstance(r["route"], list) else [r["route"]])
                  if x in doc["models"][r["model"]]["route_ids"]]
        if routes:
            kept.append(dict(r, route=routes))
    doc["rows"] = kept
    return doc


def test_show_lists_models_with_routability_and_verify_marks(override_path, capsys):
    assert run("show", "--as-of", "2026-10-13") == 0
    out = capsys.readouterr().out
    assert "dataset_version 2026-10-13" in out
    assert "anthropic/claude-sonnet-5.5" in out and "(not routable)" in out


def test_check_lists_unconfirmed_prices_unroutable_models_and_aliases(override_path, capsys):
    assert run("check", "--as-of", "2026-10-13") == 1
    out = capsys.readouterr().out
    assert "price-verify" in out and "anthropic/claude-haiku-5.5 (openrouter)" in out
    assert "not-routable" in out and "alias" in out


def test_a_row_past_expires_after_days_is_listed_and_exits_1(override_path, capsys):
    assert run("check", "--as-of", "2027-02-01") == 1
    assert "stale-row" in capsys.readouterr().out
    assert run("check", "--as-of", "2026-10-13", "--json") == 1
    kinds = {f["kind"] for f in json.loads(capsys.readouterr().out)}
    assert "stale-row" not in kinds


def test_price_window_closing_enters_the_list_from_2026_12_02(override_path, capsys):
    run("check", "--as-of", "2026-12-01")
    assert "price-window-closing" not in capsys.readouterr().out
    run("check", "--as-of", "2026-12-02")
    out = capsys.readouterr().out
    assert "price-window-closing" in out and "google/gemini-3.8-flash" in out


def test_a_clean_database_exits_0(override_path, monkeypatch, capsys):
    monkeypatch.setattr(scores_db, "load_shipped", clean_shipped)
    assert run("check", "--as-of", "2026-10-13") == 0
    assert "clean" in capsys.readouterr().out


def test_diff_shows_only_your_overrides(override_path, capsys):
    assert run("diff") == 0 and "No user overrides" in capsys.readouterr().out
    write_override(override_path, [ROW])
    assert run("diff") == 0
    out = capsys.readouterr().out
    assert "override-changed" in out and "tester anthropic/claude-haiku-5.5" in out


def test_an_override_older_than_the_shipped_row_is_listed_by_check(override_path, capsys):
    write_override(override_path, [dict(ROW, as_of="2026-01-05")])
    run("check", "--as-of", "2026-10-13")
    assert "override-older-than-shipped" in capsys.readouterr().out


# --- refresh: proposals only --------------------------------------------------------------------------
def proposal_file(tmp_path, **kw):
    p = tmp_path / "proposals.yaml"
    p.write_text(yaml.safe_dump({"rows": [dict(ROW, **kw)]}))
    return p


def test_refresh_without_a_terminal_lists_proposals_and_changes_nothing(override_path, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(models_cmd, "_can_prompt", lambda args: False)
    assert run("refresh", "--from", str(proposal_file(tmp_path))) == 10
    out = capsys.readouterr().out
    assert "approval pending" in out and "[set]" in out
    assert not override_path.exists()


def test_refresh_approve_writes_atomically_owner_only_and_keeps_other_overrides(override_path, tmp_path, monkeypatch):
    other = dict(ROW, role="planner", score=6.0)
    write_override(override_path, [other])
    monkeypatch.setattr(models_cmd, "_can_prompt", lambda args: True)
    monkeypatch.setattr(ui, "select", lambda message, choices, default=None, **k: "approve")
    assert run("refresh", "--from", str(proposal_file(tmp_path))) == 0
    doc = yaml.safe_load(override_path.read_text())
    roles = sorted(r["role"] for r in doc["rows"])
    assert roles == ["planner", "tester"]
    assert stat.S_IMODE(override_path.stat().st_mode) == 0o600
    assert not override_path.with_name(override_path.name + ".tmp").exists()


def test_refresh_skip_and_never_never_auto_approve(override_path, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(models_cmd, "_can_prompt", lambda args: True)
    monkeypatch.setattr(ui, "select", lambda message, choices, default=None, **k: "skip")
    assert run("refresh", "--from", str(proposal_file(tmp_path))) == 0
    assert not override_path.exists()
    monkeypatch.setattr(ui, "select", lambda message, choices, default=None, **k: "never")
    assert run("refresh", "--from", str(proposal_file(tmp_path))) == 0
    assert yaml.safe_load(override_path.read_text())["declined"] == [["tester", "anthropic/claude-haiku-5.5",
                                                                      "anthropic-direct", "default"]]
    capsys.readouterr()
    assert run("refresh", "--from", str(proposal_file(tmp_path))) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_refresh_honours_the_models_lock(override_path, tmp_path, monkeypatch, capsys):
    @contextlib.contextmanager
    def busy(path=None):
        yield False
    monkeypatch.setattr(models, "update_lock", busy)
    monkeypatch.setattr(models_cmd, "_can_prompt", lambda args: True)
    monkeypatch.setattr(ui, "select", lambda *a, **k: pytest.fail("asked while the lock was held"))
    assert run("refresh", "--from", str(proposal_file(tmp_path))) == models.EXIT_LOCKED
    assert not override_path.exists()


def test_refresh_proposes_dropping_an_override_a_newer_shipped_row_beats(override_path, monkeypatch, capsys):
    write_override(override_path, [dict(ROW, as_of="2026-01-05")])
    monkeypatch.setattr(models_cmd, "_can_prompt", lambda args: False)
    assert run("refresh") == 10
    assert "[drop]" in capsys.readouterr().out
    assert yaml.safe_load(override_path.read_text())["rows"]          # still there
