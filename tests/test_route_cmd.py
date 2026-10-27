"""`ai-resources route <role>` (ADR-0019): resolves through the matrix with pool usage and writer families."""
from __future__ import annotations

import json

import pytest

from ai_resources import cli
from ai_resources.scoring import routing
from ai_resources.setup import detection


@pytest.fixture(autouse=True)
def _agy_signed_in(monkeypatch):
    monkeypatch.setattr(detection, "agy_active", lambda: True)       # the host's agy/codex state never reaches a test
    monkeypatch.setattr(detection, "codex_active", lambda: False)


def _run(capsys, *argv):
    rc = cli.main(["route", *argv])
    return rc, capsys.readouterr()


def _usage(tmp_path, **pools):
    f = tmp_path / "usage.json"
    f.write_text(json.dumps({p.replace("_", "-"): v for p, v in pools.items()}))
    return str(f)


def test_json_output_for_the_primary(tmp_path, capsys):
    rc, out = _run(capsys, "implementer", "--usage-file", _usage(tmp_path, claude_subscription=0.2), "--json")
    doc = json.loads(out.out)
    assert rc == 0 and doc["cell"] == "claude-code:sonnet" and doc["pool"] == routing.POOL_MAX
    assert doc["cause"] is None and doc["blocked"] is False and doc["fallbacks"] == ["agy:gemini-4-argon-high"]


def test_a_pool_at_85_percent_spills_with_a_cause(tmp_path, capsys):
    rc, out = _run(capsys, "implementer", "--usage-file", _usage(tmp_path, claude_subscription=0.85))
    assert rc == 0 and "agy:gemini-4-argon-high" in out.out and "cause: pool-hot" in out.out


def test_writers_steer_the_checker(tmp_path, capsys):
    f = _usage(tmp_path)
    rc, out = _run(capsys, "verifier", "--usage-file", f, "--json")
    assert json.loads(out.out)["cell"] == "claude-code:sonnet"
    rc, out = _run(capsys, "verifier", "--usage-file", f, "--writers", "sonnet", "--json")
    assert json.loads(out.out)["cell"] == "claude-code:opus"


def test_blocked_checker_exits_3(tmp_path, capsys):
    rc, out = _run(capsys, "verifier", "--usage-file", _usage(tmp_path), "--writers", "sonnet,opus")
    assert rc == 3 and "BLOCKED" in out.out


def test_unavailable_backend_records_its_cause(tmp_path, capsys):
    rc, out = _run(capsys, "explore", "--usage-file", _usage(tmp_path), "--unavailable", "agy=agy-missing", "--json")
    doc = json.loads(out.out)
    assert rc == 0 and doc["cell"] == "claude-code:haiku" and doc["cause"] == "agy-missing"


def test_bad_input_exits_2(tmp_path, capsys):
    assert _run(capsys, "nope", "--usage-file", _usage(tmp_path))[0] == 2
    assert _run(capsys, "explore", "--usage-file", str(tmp_path / "missing.json"))[0] == 2


def test_every_role_routes(tmp_path, capsys):
    f = _usage(tmp_path)
    for role in routing.ROLES:
        assert _run(capsys, role, "--usage-file", f)[0] == 0, role


def test_refresh_syncs_the_usage_caches_before_reading_them(monkeypatch):
    from ai_resources import pool_usage
    calls = []
    monkeypatch.setattr(pool_usage, "sync_caches", lambda *a, **k: calls.append(1) or {"updated": [], "errors": {}})
    monkeypatch.setattr(pool_usage, "snapshot", lambda *a, **k: {})
    assert cli.main(["route", "explore", "--refresh", "--json"]) == 0 and calls == [1]
